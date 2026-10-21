"""Channel adapters: what each source offers, reported as facts only.

An adapter states, per upstream id, what the source itself calls the model,
which call traits that id fixes, and which ids are the provider's own
products. Public names, merging and listing are decided by the pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping

from .names import EFFORTS, compare_key, display_traits, suffix_traits


@dataclass(frozen=True)
class Offer:
    source: str
    provider: str
    wire: str
    name: str
    basis: str = "id"
    traits: Mapping[str, str] = field(default_factory=dict)
    efforts: tuple[str, ...] = ()
    thinking: str = ""
    product: bool = False
    available: bool = True
    legacy: str = ""


@dataclass(frozen=True)
class Adapter:
    """``basis``: whose word names the model - the upstream ``id``, its display ``name``, or ``both``."""
    basis: str = "id"
    products: frozenset[str] = frozenset()
    product_prefixes: tuple[str, ...] = ()
    usable: Callable[[dict, str], bool] | None = None


def _antigravity_usable(account: dict, wire: str) -> bool:
    from ..oauth import antigravity
    return antigravity.model_at_generation_endpoint(account, wire)


ADAPTERS: dict[str, Adapter] = {
    "qoder": Adapter(basis="name", products=frozenset({
        "ultimate", "performance", "fast", "efficient", "smodel", "cmodel", "auto", "default"})),
    "antigravity": Adapter(basis="name", usable=_antigravity_usable),
    "workbuddy": Adapter(basis="both", products=frozenset({
        "default-model", "fast-model", "balanced-model", "primary-model", "auto", "default"})),
    "cursor": Adapter(products=frozenset({"default"}), product_prefixes=("composer-",)),
}
_DEFAULT = Adapter()


def _record_capabilities(record: Mapping[str, Any]) -> tuple[tuple[str, ...], str]:
    raw = [str(item).strip().lower() for item in record.get("reasoningEfforts") or [] if str(item).strip()]
    efforts = tuple(dict.fromkeys(item if item != "extra-high" else "xhigh" for item in raw if item in EFFORTS or item == "extra-high"))
    if record.get("qoderAlwaysThinks") is True or record.get("onlyReasoning") is True:
        return efforts, "always"
    if {"enabled", "disabled"} & set(raw) or record.get("canDisableThinking") is True:
        return efforts, "toggle"
    return efforts, ""


def _traits(adapter: Adapter, wire: str, name: str) -> dict[str, str]:
    base, traits = display_traits(name)
    if adapter.basis == "name":
        # A name-based source may encode traits only in its id (``-disabled``);
        # trust the id's suffix only when it names the same model as the display.
        id_base, id_traits = suffix_traits(wire)
        if compare_key(id_base) == compare_key(base):
            traits = {**id_traits, **traits}
    return traits


def account_offers(account: dict) -> list[Offer]:
    from .. import oauth_manager
    from ..image_catalog import is_image_name
    from ..oauth_ids import account_key
    provider = oauth_manager.provider_of(account)
    adapter = ADAPTERS.get(provider, _DEFAULT)
    selection = oauth_manager.account_model_selection(account)
    records = {str(item.get("id")): item for item in selection.get("records") or [] if isinstance(item, dict)}
    effective = set(selection.get("effective_models") or ())
    source = "oauth:" + account_key(account)
    result = []
    for wire in selection.get("models") or ():
        wire = str(wire or "").strip()
        if not wire or is_image_name(wire):
            continue
        record = records.get(wire, {})
        name = str(record.get("name") or wire).strip()
        efforts, thinking = _record_capabilities(record)
        product = wire in adapter.products or any(wire.startswith(prefix) for prefix in adapter.product_prefixes)
        usable = wire in effective and (adapter.usable is None or adapter.usable(account, wire))
        result.append(Offer(source=source, provider=provider, wire=wire, name=name, basis=adapter.basis,
                            traits=_traits(adapter, wire, name), efforts=efforts, thinking=thinking,
                            product=product, available=usable))
    return result


def channel_offers(channel: dict) -> list[Offer]:
    """API channels: the configured public alias is the admin's own name for the model."""
    from ..image_catalog import is_image_name
    name = str(channel.get("name") or "").strip()
    result = []
    for item in channel.get("models") or ():
        if isinstance(item, dict):
            wire, alias = str(item.get("real") or "").strip(), str(item.get("alias") or item.get("real") or "").strip()
        else:
            wire = alias = str(item or "").strip()
        if not wire or not alias or is_image_name(wire):
            continue
        result.append(Offer(source="api:" + name, provider="api", wire=wire, name=alias, basis="name"))
    return result


def collect(cfg: Mapping[str, Any]) -> list[Offer]:
    offers: list[Offer] = []
    for account in cfg.get("oauthAccounts") or ():
        if isinstance(account, dict):
            try:
                offers.extend(account_offers(account))
            except (KeyError, ValueError, TypeError):
                continue
    channels = cfg.get("channels") or ()
    for channel in channels.values() if isinstance(channels, Mapping) else channels:
        if isinstance(channel, dict):
            offers.extend(channel_offers(channel))
    return offers


def with_legacy(offers: Iterable[Offer], legacy: Mapping[tuple[str, str], str]) -> list[Offer]:
    """Attach today's public name of each (source, wire), when it has one."""
    return [replace(offer, legacy=legacy.get((offer.source, offer.wire), "")) for offer in offers]
