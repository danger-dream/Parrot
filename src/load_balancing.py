"""负载均衡模式与 priority 顺序配置。

Priority 调度使用两层统一顺序：

1. ``loadBalancing.modelPriorityOrders[client_model]``：模型专属顺序；
2. ``loadBalancing.channelPriorityOrder``：所有账户/渠道的默认顺序。

模型顺序粒度更细，始终高于统一渠道顺序。协议转换能力、禁用、冷却、
并发等候选过滤仍由 scheduler/matrix 在排序前完成；亲和仍在排序后生效。

旧版 ``priorityOrders.anthropic/openai`` 只用于首次迁移和兼容读取，不再作为
Telegram 的配置入口。迁移时保持每个旧家族内部相对顺序，并以 registry 原始
顺序打破相同 family rank，尽量接近旧版的实际行为。
"""

from __future__ import annotations

from typing import Iterable, Mapping

from . import config
from .channel.base import Channel

FAMILIES = ("anthropic", "openai")


def family_for_protocol(protocol: str | None) -> str:
    """协议名 → 兼容/状态统计家族。Priority 排序本身不再按家族分组。"""
    return "anthropic" if (protocol or "anthropic") == "anthropic" else "openai"


def family_for_channel(ch: Channel) -> str:
    return family_for_protocol(getattr(ch, "protocol", "anthropic"))


def display_mode(mode: str | None) -> str:
    m = (mode or "smart").lower()
    if m == "smart":
        return "智能调度"
    if m == "order":
        return "顺序调度"
    if m == "priority":
        return "优先级调度"
    return m


def mode_description(mode: str | None) -> str:
    m = (mode or "smart").lower()
    if m == "smart":
        return "按滑动窗口评分 + 20% 探索率排序"
    if m == "order":
        return "按配置顺序依次尝试"
    if m == "priority":
        return "模型专属优先级 > 统一渠道/账户优先级"
    return "未知调度算法"


def _dedupe(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if value and value not in seen:
            out.append(value)
            seen.add(value)
    return out


# ─── 旧家族配置兼容 ───────────────────────────────────────────────


def _orders_from_cfg(cfg: dict) -> dict[str, list[str]]:
    lb = cfg.get("loadBalancing") or {}
    po = lb.get("priorityOrders") or {}
    return {
        "anthropic": list(po.get("anthropic") or []),
        "openai": list(po.get("openai") or []),
    }


def priority_orders() -> dict[str, list[str]]:
    """返回旧版家族表；仅供兼容测试/旧代码读取。"""
    return _orders_from_cfg(config.get())


def normalize_order_for_family(
    family: str,
    live_keys: Iterable[str],
    cfg: dict | None = None,
) -> list[str]:
    """旧版 family 顺序归一化，不写配置。"""
    cfg = cfg or config.get()
    live = _dedupe(live_keys)
    live_set = set(live)
    saved = _orders_from_cfg(cfg).get(family, [])
    out = [key for key in _dedupe(saved) if key in live_set]
    seen = set(out)
    out.extend(key for key in live if key not in seen)
    return out


def normalize_orders_from_channels(
    channels: Iterable[Channel],
    cfg: dict | None = None,
) -> dict[str, list[str]]:
    """旧版两个 family 表归一化；保留用于平滑迁移。"""
    cfg = cfg or config.get()
    live_by_family = {"anthropic": [], "openai": []}
    for ch in channels:
        live_by_family.setdefault(family_for_channel(ch), []).append(ch.key)
    return {
        family: normalize_order_for_family(
            family, live_by_family.get(family, []), cfg,
        )
        for family in FAMILIES
    }


def save_family_order(family: str, order: list[str]) -> None:
    """旧 API 兼容。新 UI 不再调用。"""
    if family not in FAMILIES:
        raise ValueError(f"unsupported family: {family}")
    clean = _dedupe(order)

    def mutate(cfg: dict) -> None:
        lb = cfg.setdefault("loadBalancing", {})
        lb["initialized"] = True
        lb.setdefault("priorityOrders", {})[family] = clean

    config.update(mutate)


# ─── 统一渠道 + 模型专属顺序 ──────────────────────────────────────


def _model_orders_from_cfg(cfg: dict) -> dict[str, list[str]]:
    raw = (cfg.get("loadBalancing") or {}).get("modelPriorityOrders") or {}
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(model): _dedupe(order if isinstance(order, list) else [])
        for model, order in raw.items()
        if str(model or "").strip()
    }


def model_priority_orders(cfg: dict | None = None) -> dict[str, list[str]]:
    return _model_orders_from_cfg(cfg or config.get())


def has_model_priority(model: str, cfg: dict | None = None) -> bool:
    return str(model or "").strip() in _model_orders_from_cfg(cfg or config.get())


def _legacy_merged_order(channels: list[Channel], cfg: dict) -> list[str]:
    """把旧 family ranks 合成一个稳定初始顺序。"""
    if not channels:
        return []
    live_by_family: dict[str, list[str]] = {"anthropic": [], "openai": []}
    registry_index: dict[str, int] = {}
    channel_by_key: dict[str, Channel] = {}
    for index, ch in enumerate(channels):
        registry_index[ch.key] = index
        channel_by_key[ch.key] = ch
        live_by_family.setdefault(family_for_channel(ch), []).append(ch.key)
    family_orders = {
        family: normalize_order_for_family(family, keys, cfg)
        for family, keys in live_by_family.items()
    }
    family_rank = {
        (family, key): rank
        for family, order in family_orders.items()
        for rank, key in enumerate(order)
    }
    keys = [ch.key for ch in channels]
    return sorted(
        _dedupe(keys),
        key=lambda key: (
            family_rank.get(
                (family_for_channel(channel_by_key[key]), key), 1_000_000,
            ),
            registry_index.get(key, 1_000_000),
        ),
    )


def normalize_channel_order(
    channels: Iterable[Channel] | None = None,
    cfg: dict | None = None,
) -> list[str]:
    """返回全部 live 账户/渠道的统一默认顺序，不写配置。"""
    cfg = cfg or config.get()
    if channels is None:
        from .channel import registry
        channels = registry.all_channels()
    live_channels = list(channels)
    live_keys = _dedupe(ch.key for ch in live_channels)
    live_set = set(live_keys)
    lb = cfg.get("loadBalancing") or {}
    raw_saved = lb.get("channelPriorityOrder")
    if isinstance(raw_saved, list) and raw_saved:
        saved = [key for key in _dedupe(raw_saved) if key in live_set]
        seen = set(saved)
        saved.extend(key for key in live_keys if key not in seen)
        return saved
    return _legacy_merged_order(live_channels, cfg)


def channel_priority_order(cfg: dict | None = None) -> list[str]:
    return normalize_channel_order(cfg=cfg)


def effective_order_for_model(
    model: str,
    channels: Iterable[Channel],
    cfg: dict | None = None,
) -> list[str]:
    """返回一个模型的完整有效顺序。

    模型专属表中的 live 项优先；该模型未列的新渠道按统一渠道顺序追加。
    没有专属表时完整继承统一渠道顺序。
    """
    cfg = cfg or config.get()
    live_channels = list(channels)
    live_keys = _dedupe(ch.key for ch in live_channels)
    live_set = set(live_keys)
    global_order = normalize_channel_order(cfg=cfg)
    global_rank = {key: index for index, key in enumerate(global_order)}
    registry_rank = {key: index for index, key in enumerate(live_keys)}

    model_id = str(model or "").strip()
    orders = _model_orders_from_cfg(cfg)
    if model_id in orders:
        selected = [key for key in orders[model_id] if key in live_set]
        seen = set(selected)
        remaining = [key for key in live_keys if key not in seen]
        remaining.sort(key=lambda key: (
            global_rank.get(key, 1_000_000 + registry_rank[key]),
            registry_rank[key],
        ))
        return selected + remaining

    return sorted(live_keys, key=lambda key: (
        global_rank.get(key, 1_000_000 + registry_rank[key]),
        registry_rank[key],
    ))


def sort_candidates_by_priority(
    candidates: list[tuple[Channel, str]],
    cfg: dict | None = None,
    requested_model: str | None = None,
) -> list[tuple[Channel, str]]:
    """按模型专属 > 统一渠道默认顺序排列候选。"""
    if len(candidates) <= 1:
        return candidates
    cfg = cfg or config.get()
    channels = [ch for ch, _resolved in candidates]
    order = effective_order_for_model(requested_model or "", channels, cfg)
    rank = {key: index for index, key in enumerate(order)}
    return [
        item for _index, item in sorted(
            enumerate(candidates),
            key=lambda indexed: (
                rank.get(indexed[1][0].key, 1_000_000 + indexed[0]),
                indexed[0],
            ),
        )
    ]


def is_initialized(cfg: dict | None = None) -> bool:
    cfg = cfg or config.get()
    return bool((cfg.get("loadBalancing") or {}).get("initialized"))


def initialize_priority_orders() -> list[str]:
    """迁移/初始化统一渠道顺序并持久化。"""
    from .channel import registry

    channels = registry.all_channels()

    def mutate(cfg: dict) -> None:
        lb = cfg.setdefault("loadBalancing", {})
        lb["initialized"] = True
        lb["channelPriorityOrder"] = normalize_channel_order(channels, cfg)
        lb["priorityOrders"] = normalize_orders_from_channels(channels, cfg)
        lb.setdefault("modelPriorityOrders", {})

    updated = config.update(mutate)
    return list((updated.get("loadBalancing") or {}).get("channelPriorityOrder") or [])


def set_mode(mode: str) -> None:
    mode = (mode or "").lower()
    if mode not in ("smart", "order", "priority"):
        raise ValueError(f"unsupported channelSelection mode: {mode}")
    cfg = config.get()
    lb = cfg.get("loadBalancing") or {}
    if mode == "priority" and not (lb.get("channelPriorityOrder") or []):
        initialize_priority_orders()
    config.update(lambda current: current.__setitem__("channelSelection", mode))


def save_channel_order(order: list[str]) -> None:
    clean = _dedupe(order)

    def mutate(cfg: dict) -> None:
        lb = cfg.setdefault("loadBalancing", {})
        lb["initialized"] = True
        lb["channelPriorityOrder"] = clean
        lb.setdefault("modelPriorityOrders", {})

    config.update(mutate)


def save_model_order(model: str, order: list[str]) -> None:
    model_id = str(model or "").strip()
    if not model_id:
        raise ValueError("model is required")
    save_model_orders({model_id: order})


def save_model_orders(orders: Mapping[str, list[str]]) -> None:
    clean = {
        str(model).strip(): _dedupe(order)
        for model, order in orders.items()
        if str(model or "").strip()
    }
    if not clean:
        return

    def mutate(cfg: dict) -> None:
        lb = cfg.setdefault("loadBalancing", {})
        lb["initialized"] = True
        target = lb.setdefault("modelPriorityOrders", {})
        for model, order in clean.items():
            target[model] = order

    config.update(mutate)


def clear_model_orders(models: Iterable[str]) -> int:
    wanted = {str(model or "").strip() for model in models if str(model or "").strip()}
    if not wanted:
        return 0
    removed = {"count": 0}

    def mutate(cfg: dict) -> None:
        target = cfg.setdefault("loadBalancing", {}).setdefault("modelPriorityOrders", {})
        for model in wanted:
            if model in target:
                target.pop(model, None)
                removed["count"] += 1

    config.update(mutate, skip_if_unchanged=True)
    return removed["count"]


# ─── 渠道生命周期同步 ─────────────────────────────────────────────


def sync_channel_added(channel_key: str, family: str) -> bool:
    """已初始化时把新渠道追加统一队尾；同时维护旧 family 表兼容。"""
    if not channel_key:
        return False
    cfg = config.get()
    if not is_initialized(cfg):
        return False
    changed = {"value": False}

    def mutate(current: dict) -> None:
        lb = current.setdefault("loadBalancing", {})
        unified = lb.get("channelPriorityOrder")
        if isinstance(unified, list) and unified and channel_key not in unified:
            unified.append(channel_key)
            changed["value"] = True
        if family in FAMILIES:
            legacy = lb.setdefault("priorityOrders", {}).setdefault(family, [])
            if channel_key not in legacy:
                legacy.append(channel_key)
                changed["value"] = True

    config.update(mutate, skip_if_unchanged=True)
    return changed["value"]


def sync_channel_removed(channel_key: str) -> bool:
    if not channel_key:
        return False
    cfg = config.get()
    if not is_initialized(cfg):
        return False
    changed = {"value": False}

    def mutate(current: dict) -> None:
        changed["value"] = mutate_channels_removed(current, {channel_key})

    config.update(mutate, skip_if_unchanged=True)
    return changed["value"]


def mutate_channels_removed(cfg: dict, channel_keys: set[str]) -> bool:
    if not channel_keys:
        return False
    changed = False
    lb = cfg.setdefault("loadBalancing", {})

    unified = list(lb.get("channelPriorityOrder") or [])
    clean_unified = [key for key in unified if key not in channel_keys]
    if clean_unified != unified:
        lb["channelPriorityOrder"] = clean_unified
        changed = True

    legacy = lb.setdefault("priorityOrders", {})
    for family in FAMILIES:
        current = list(legacy.get(family) or [])
        clean = [key for key in current if key not in channel_keys]
        if clean != current:
            legacy[family] = clean
            changed = True

    model_orders = lb.setdefault("modelPriorityOrders", {})
    for model, raw_order in list(model_orders.items()):
        current = list(raw_order or [])
        clean = [key for key in current if key not in channel_keys]
        if clean != current:
            if clean:
                model_orders[model] = clean
            else:
                model_orders.pop(model, None)
            changed = True
    return changed


def sync_channel_renamed(old_key: str, new_key: str, family: str) -> bool:
    if not old_key or not new_key or old_key == new_key:
        return False
    cfg = config.get()
    if not is_initialized(cfg):
        return False
    changed = {"value": False}

    def mutate(current: dict) -> None:
        changed["value"] = mutate_channel_renamed(
            current, old_key, new_key, family,
        )

    config.update(mutate, skip_if_unchanged=True)
    return changed["value"]


def _replace_key(order: Iterable[str], old_key: str, new_key: str) -> list[str]:
    return _dedupe(new_key if key == old_key else key for key in order)


def mutate_channel_renamed(
    cfg: dict,
    old_key: str,
    new_key: str,
    family: str,
) -> bool:
    lb = cfg.setdefault("loadBalancing", {})
    changed = False

    unified = list(lb.get("channelPriorityOrder") or [])
    if unified:
        replaced = _replace_key(unified, old_key, new_key)
        if new_key not in replaced:
            replaced.append(new_key)
        if replaced != unified:
            lb["channelPriorityOrder"] = replaced
            changed = True

    model_orders = lb.setdefault("modelPriorityOrders", {})
    for model, raw_order in list(model_orders.items()):
        current = list(raw_order or [])
        replaced = _replace_key(current, old_key, new_key)
        if replaced != current:
            model_orders[model] = replaced
            changed = True

    # Keep old family tables coherent for downgrade/compatibility.
    legacy = lb.setdefault("priorityOrders", {})
    for current_family in FAMILIES:
        current = list(legacy.get(current_family) or [])
        out: list[str] = []
        for key in current:
            if current_family == family:
                mapped = new_key if key == old_key else key
            elif key in (old_key, new_key):
                continue
            else:
                mapped = key
            if mapped not in out:
                out.append(mapped)
        if current_family == family and new_key not in out:
            out.append(new_key)
        if out != current:
            legacy[current_family] = out
            changed = True
    return changed
