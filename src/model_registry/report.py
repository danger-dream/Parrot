"""Shadow comparison: today's listing and name routing against the registry's.

Runs on channels already rebuilt from the configuration being compared. Today's
switches are carried over per (source, upstream id), so any difference shown is
caused by naming alone.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from .offers import Offer, collect, with_legacy
from .pipeline import Registry, build
from .resolver import choose, resolve


@dataclass
class Comparison:
    registry: Registry
    keys: list[str]
    old_visible: dict[str, set[str]] = field(default_factory=dict)
    new_visible: dict[str, set[str]] = field(default_factory=dict)
    routes: dict[str, dict[str, tuple[set, set]]] = field(default_factory=dict)


def legacy_names() -> dict[tuple[str, str], str]:
    from ..channel import registry as channels
    result: dict[tuple[str, str], str] = {}
    for channel in channels.all_channels():
        for name in channel.list_client_models():
            wire = channel.supports_model(name)
            if wire:
                result.setdefault((channel.key, wire), name)
    return result


def _allowed_models(key: str) -> list[str]:
    from .. import auth
    return list((auth.api_key_entry(key) or {}).get("allowedModels") or [])


def _global_map() -> dict[str, str]:
    from .. import model_mapping
    return model_mapping.get_global_map()


def old_visible(key: str) -> set[str]:
    from .. import auth
    from ..channel import registry as channels
    scoped = auth.allowed_channels(key) is not None
    models = channels.available_models(api_key_name=key) if scoped else channels.available_models()
    allowed = _allowed_models(key)
    visible = {m for m in models if not allowed or m in allowed}
    if allowed:
        visible |= channels.available_model_spellings(allowed, api_key_name=key)
    mapping = _global_map()
    aliases = channels.available_model_aliases(mapping, api_key_name=key)
    for alias, real in mapping.items():
        if alias in aliases and (real in visible) and (not allowed or alias in allowed):
            visible.add(alias)
    return visible


def old_route(name: str, key: str) -> set[tuple[str, str]]:
    from .. import auth, model_state
    from ..channel import registry as channels
    model = _global_map().get(name, name)
    allowed = _allowed_models(key)
    if allowed and model not in allowed:
        return set()
    result = set()
    for channel in channels.all_channels():
        if not auth.channel_allowed(key, channel.key) or not channel.enabled or channel.disabled_reason:
            continue
        try:
            resolver = getattr(channel, "resolve_request_model", None)
            wire = resolver(model, {"model": model}, "chat") if callable(resolver) else channel.supports_model(model)
        except Exception:
            continue
        if wire and model_state.is_global_enabled(model, source_key=channel.key) \
                and model_state.is_source_enabled(channel.key, model):
            result.add((channel.key, wire))
    return result


def offer_live(offer: Offer, model_id: str | None = None) -> bool:
    from .. import model_state
    from ..channel import registry as channels
    channel = channels.get_channel(offer.source)
    if channel is None or not channel.enabled or channel.disabled_reason or not offer.available:
        return False
    name = offer.legacy or offer.wire
    return model_state.is_global_enabled(name, source_key=offer.source) and model_state.is_source_enabled(offer.source, name)


def offer_listed(offer: Offer) -> bool:
    from .. import model_state
    return offer_live(offer) and model_state.is_discovery_visible(offer.legacy or offer.wire, source_key=offer.source)


def new_visible(registry: Registry, key: str) -> set[str]:
    from .. import auth
    allowed = _allowed_models(key)
    permitted = {found.model for name in allowed if (found := resolve(registry, name))} if allowed else None
    visible = {model.id for model in registry.models.values()
               if any(offer_listed(offer) and auth.channel_allowed(key, offer.source) for offer in model.offers)
               and (permitted is None or model.id in permitted)}
    for alias, real in _global_map().items():
        found = resolve(registry, real)
        if found and found.model in visible and (not allowed or alias in allowed):
            visible.add(alias)
    return visible


def new_route(registry: Registry, name: str, key: str) -> set[tuple[str, str]]:
    from .. import auth
    target = _global_map().get(name, name)
    found = resolve(registry, target)
    if found is None:
        return set()
    allowed = _allowed_models(key)
    if allowed and found.model not in {hit.model for item in allowed if (hit := resolve(registry, item))}:
        return set()
    choices = choose(registry, found, lambda offer: offer_live(offer) and auth.channel_allowed(key, offer.source))
    return {(choice.offer.source, choice.offer.wire) for choice in choices}


def scheduled_route(name: str, key: str) -> set[tuple[str, str]]:
    """End to end through the scheduler, as an ingress would call it after mapping and the allowlist."""
    from .. import scheduler
    from . import runtime
    model = _global_map().get(name, name)
    if not runtime.permits(_allowed_models(key), model):
        return set()
    result = scheduler.schedule({"model": model, "messages": [{"role": "user", "content": "hi"}]},
                                key, "127.0.0.1", ingress_protocol="chat")
    return {(channel.key, wire) for channel, wire in list(result.candidates) + list(result.saturated)}


def snapshot(keys: list[str], names: Iterable[str], route) -> tuple[dict, dict]:
    visible = {key: old_visible(key) for key in keys}
    routes = {name: {key: route(name, key) for key in keys} for name in sorted(set(names))}
    return visible, routes


def compare(cfg, *, rules=(), extra_names: Iterable[str] = ()) -> Comparison:
    offers = with_legacy(collect(cfg), legacy_names())
    registry = build(offers, rules=rules)
    keys = sorted((cfg.get("apiKeys") or {}).keys())
    result = Comparison(registry, keys)
    names: set[str] = set(extra_names)
    for key in keys:
        result.old_visible[key] = old_visible(key)
        result.new_visible[key] = new_visible(registry, key)
        names |= result.old_visible[key] | result.new_visible[key]
    names |= {offer.legacy for offer in offers if offer.legacy}
    for name in sorted(names):
        result.routes[name] = {key: (old_route(name, key), new_route(registry, name, key)) for key in keys}
    return result


def _source_label(source: str, cfg) -> str:
    if source.startswith("api:"):
        return source
    provider = source.split(":")[1] if source.count(":") >= 1 else source
    for account in cfg.get("oauthAccounts") or ():
        try:
            from ..oauth_ids import account_key
            if "oauth:" + account_key(account) == source:
                return f"{provider}:{account.get('label') or account.get('email') or '?'}"
        except Exception:
            continue
    return source


def render(result: Comparison, cfg, *, live=None, title: str = "影子运行差异报告", notes: Iterable[str] = ()) -> str:
    reg = result.registry
    live = live or offer_live
    label = lambda source: _source_label(source, cfg)
    lines = [f"# 统一模型管理：{title}", ""]
    notes = list(notes)
    if notes:
        lines += ["## 0. 迁移改写的开关", ""] + [f"- {note}" for note in notes] + [""]
    multi = [m for m in reg.models.values() if len({o.source for o in m.offers}) > 1 or len({o.wire for o in m.offers}) > 1]
    pending = [(m.id, p) for m in reg.models.values() for p in m.pending]
    lines += [f"- 模型 {len(reg.models)} 个，其中多来源或多写法 {len(multi)} 个；待确认 {len(pending)} 条；"
              f"别名冲突 {len(reg.conflicts)} 条；合并建议 {len(reg.suggestions)} 条。", ""]

    lines += ["## 1. 模型列表变化（按 API Key）", ""]
    groups: dict[tuple, list[str]] = defaultdict(list)
    for key in result.keys:
        removed = tuple(sorted(result.old_visible[key] - result.new_visible[key]))
        added = tuple(sorted(result.new_visible[key] - result.old_visible[key]))
        groups[(removed, added)].append(key)
    for (removed, added), keys in groups.items():
        lines.append(f"### {', '.join(keys)}")
        if not removed and not added:
            lines += ["无变化。", ""]
            continue
        for name in removed:
            found = resolve(reg, name)
            target = f"仍可调用 → `{found.model}`" + (f"（{found.traits}）" if found and found.traits else "") if found else "**不再可调用**"
            lines.append(f"- 移出 `{name}`：{target}")
        for name in added:
            lines.append(f"- 新增 `{name}`")
        lines.append("")

    lines += ["## 2. 按名字路由的变化", "",
              "旧 = 现在的代码；新 = 统一注册表（未传强度时按 high 选档）。只列有变化的名字；同一变化在多个 Key 上只列一次。", ""]
    lost_lines, gained_lines, changed_lines = [], [], []
    for name, per_key in result.routes.items():
        seen = set()
        for key, (old, new) in per_key.items():
            if old == new:
                continue
            signature = (frozenset(old), frozenset(new))
            if signature in seen:
                continue
            seen.add(signature)
            keys = [k for k, pair in per_key.items() if (frozenset(pair[0]), frozenset(pair[1])) == signature]
            gained = sorted(new - old)
            lost = sorted(old - new)
            fmt = lambda items: "、".join(f"{label(s)} `{w}`" for s, w in items)
            text = f"- `{name}`（{', '.join(keys)}）"
            if lost and not gained:
                lost_lines.append(text + f"：失去 {fmt(lost)}")
            elif gained and not lost:
                gained_lines.append(text + f"：新增 {fmt(gained)}")
            else:
                changed_lines.append(text + f"：失去 {fmt(lost) or '无'}；新增 {fmt(gained) or '无'}")
    lines += ["### 2.1 失去来源（需逐条确认）", ""] + (lost_lines or ["无。"]) + [""]
    lines += ["### 2.2 来源改变", ""] + (changed_lines or ["无。"]) + [""]
    lines += ["### 2.3 新增来源（合并带来的）", ""] + (gained_lines or ["无。"]) + [""]

    lines += ["## 3. 待确认", ""]
    lines += [f"- {label(p.offer.source)} `{p.offer.wire}`（暂按 `{model}`）：{p.reason}" for model, p in pending] or ["无。"]
    lines += ["", "## 4. 合并建议", ""] + ([f"- {item}" for item in reg.suggestions] or ["无。"])
    lines += ["", "## 5. 别名冲突", ""] + ([f"- {item}" for item in reg.conflicts] or ["无。"])
    lines += ["", "## 6. 多来源或多写法的模型", "", "| 模型 | 显示名 | 来源 → 上游 ID |", "|---|---|---|"]
    for model in sorted(multi, key=lambda m: m.id):
        parts = []
        for offer in model.offers:
            traits = ",".join(f"{k}={v}" for k, v in offer.traits.items())
            parts.append(f"{label(offer.source)} → `{offer.wire}`" + (f"[{traits}]" if traits else "")
                         + ("" if live(offer, model.id) else "（停用）"))
        lines.append(f"| `{model.id}` | {model.name} | {'<br>'.join(parts)} |")
    lines.append("")
    return "\n".join(lines)
