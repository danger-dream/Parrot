"""负载均衡 / 调度优先级配置工具。

本模块只处理配置层和顺序归一化，不直接依赖 Telegram。

概念
----
- channelSelection:
  - smart    智能调度（评分 + 探索）
  - order    顺序调度（registry/config 原始顺序）
  - priority 用户自定义优先级顺序
- loadBalancing.priorityOrders:
  按 family 保存 channel.key 列表。family 取值：anthropic / openai。

兼容策略
--------
老配置可能没有 loadBalancing，或表里只包含部分渠道。运行期使用
``saved ∩ live + live - saved`` 的归一化顺序；只有用户显式保存/初始化时才写回配置。
"""

from __future__ import annotations

from typing import Iterable

from . import config
from .channel.base import Channel

FAMILIES = ("anthropic", "openai")


def family_for_protocol(protocol: str | None) -> str:
    """协议名 → 调度家族。"""
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
        return "按用户自行设定的优先级"
    return "未知调度算法"


def _orders_from_cfg(cfg: dict) -> dict[str, list[str]]:
    lb = cfg.get("loadBalancing") or {}
    po = lb.get("priorityOrders") or {}
    return {
        "anthropic": list(po.get("anthropic") or []),
        "openai": list(po.get("openai") or []),
    }


def priority_orders() -> dict[str, list[str]]:
    """返回当前配置里的优先级表（缺省补空表）。"""
    return _orders_from_cfg(config.get())


def is_initialized(cfg: dict | None = None) -> bool:
    cfg = cfg or config.get()
    lb = cfg.get("loadBalancing") or {}
    return bool(lb.get("initialized"))


def normalize_order_for_family(family: str, live_keys: Iterable[str],
                               cfg: dict | None = None) -> list[str]:
    """返回内存归一化后的 family 顺序，不写配置。

    规则：配置里仍存在的项保持原顺序；配置缺失但当前 live 的项追加到末尾。
    """
    cfg = cfg or config.get()
    live = list(live_keys)
    live_set = set(live)
    saved = _orders_from_cfg(cfg).get(family, [])
    out: list[str] = []
    seen: set[str] = set()
    for key in saved:
        if key in live_set and key not in seen:
            out.append(key)
            seen.add(key)
    for key in live:
        if key not in seen:
            out.append(key)
            seen.add(key)
    return out


def normalize_orders_from_channels(channels: Iterable[Channel],
                                   cfg: dict | None = None) -> dict[str, list[str]]:
    """按当前 registry 渠道列表生成完整归一化优先级表。"""
    cfg = cfg or config.get()
    live_by_family = {"anthropic": [], "openai": []}
    for ch in channels:
        fam = family_for_channel(ch)
        live_by_family.setdefault(fam, []).append(ch.key)
    return {
        fam: normalize_order_for_family(fam, live_by_family.get(fam, []), cfg)
        for fam in FAMILIES
    }


def sort_candidates_by_priority(candidates: list[tuple[Channel, str]],
                                cfg: dict | None = None) -> list[tuple[Channel, str]]:
    """按用户优先级表排序候选；表外项按原候选顺序追加。"""
    if len(candidates) <= 1:
        return candidates
    cfg = cfg or config.get()
    # candidates 已经过 family 过滤；正常只会有一个 family。这里仍按 channel family 逐项算 rank。
    live_by_family: dict[str, list[str]] = {"anthropic": [], "openai": []}
    for ch, _ in candidates:
        live_by_family.setdefault(family_for_channel(ch), []).append(ch.key)
    order_by_family = {
        fam: normalize_order_for_family(fam, live, cfg)
        for fam, live in live_by_family.items()
    }
    rank: dict[str, int] = {}
    for fam, order in order_by_family.items():
        for i, key in enumerate(order):
            rank[f"{fam}:{key}"] = i

    def _key(item):
        idx, (ch, _model) = item
        fam = family_for_channel(ch)
        return (rank.get(f"{fam}:{ch.key}", 1_000_000 + idx), idx)

    return [item for _idx, item in sorted(enumerate(candidates), key=_key)]


def initialize_priority_orders() -> dict[str, list[str]]:
    """用当前 registry 初始化并持久化优先级表。幂等。"""
    from .channel import registry  # 延迟 import 避免循环

    channels = registry.all_channels()

    def _mutate(cfg: dict) -> None:
        orders = normalize_orders_from_channels(channels, cfg)
        lb = cfg.setdefault("loadBalancing", {})
        lb["initialized"] = True
        lb["priorityOrders"] = orders

    new_cfg = config.update(_mutate)
    return _orders_from_cfg(new_cfg)


def set_mode(mode: str) -> None:
    """设置 channelSelection；切到 priority 时自动初始化优先级表。"""
    mode = (mode or "").lower()
    if mode not in ("smart", "order", "priority"):
        raise ValueError(f"unsupported channelSelection mode: {mode}")
    if mode == "priority" and not is_initialized():
        initialize_priority_orders()
    config.update(lambda c: c.__setitem__("channelSelection", mode))


def save_family_order(family: str, order: list[str]) -> None:
    if family not in FAMILIES:
        raise ValueError(f"unsupported family: {family}")
    clean: list[str] = []
    seen: set[str] = set()
    for key in order:
        if key and key not in seen:
            clean.append(key)
            seen.add(key)

    def _mutate(cfg: dict) -> None:
        lb = cfg.setdefault("loadBalancing", {})
        lb["initialized"] = True
        po = lb.setdefault("priorityOrders", {})
        po[family] = clean

    config.update(_mutate)


def sync_channel_added(channel_key: str, family: str) -> bool:
    """若优先级表已初始化，把新渠道追加到队尾。返回是否写入。"""
    if family not in FAMILIES or not channel_key:
        return False
    cfg = config.get()
    if not is_initialized(cfg):
        return False

    changed = {"v": False}

    def _mutate(c: dict) -> None:
        lb = c.setdefault("loadBalancing", {})
        po = lb.setdefault("priorityOrders", {})
        arr = po.setdefault(family, [])
        if channel_key not in arr:
            arr.append(channel_key)
            changed["v"] = True

    config.update(_mutate)
    return changed["v"]


def sync_channel_removed(channel_key: str) -> bool:
    """若优先级表已初始化，从所有 family 移除渠道。返回是否写入。"""
    if not channel_key:
        return False
    cfg = config.get()
    if not is_initialized(cfg):
        return False

    changed = {"v": False}

    def _mutate(c: dict) -> None:
        changed["v"] = mutate_channels_removed(c, {channel_key})

    config.update(_mutate)
    return changed["v"]


def mutate_channels_removed(cfg: dict, channel_keys: set[str]) -> bool:
    """Pure config mutation for atomically removing priority-order entries."""
    if not channel_keys:
        return False
    changed = False
    lb = cfg.setdefault("loadBalancing", {})
    po = lb.setdefault("priorityOrders", {})
    for fam in FAMILIES:
        arr = list(po.get(fam) or [])
        new = [key for key in arr if key not in channel_keys]
        if new != arr:
            po[fam] = new
            changed = True
    return changed


def sync_channel_renamed(old_key: str, new_key: str, family: str) -> bool:
    """API 渠道改名时维护优先级表。

    `family` 是改名后的目标 family。若用户同时改名 + 改协议，旧 family
    里必须删除旧 key（以及可能残留的新 key），不能把 new_key 留在跨
    family 的 priorityOrders 里。
    """
    if not old_key or not new_key or old_key == new_key:
        return False
    cfg = config.get()
    if not is_initialized(cfg):
        return False
    changed = {"v": False}

    def _mutate(c: dict) -> None:
        changed["v"] = mutate_channel_renamed(c, old_key, new_key, family)

    config.update(_mutate)
    return changed["v"]


def mutate_channel_renamed(cfg: dict, old_key: str, new_key: str,
                           family: str) -> bool:
    """Pure config mutation used when a caller must publish rename atomically."""
    lb = cfg.setdefault("loadBalancing", {})
    po = lb.setdefault("priorityOrders", {})
    changed = False
    for fam in FAMILIES:
        arr = list(po.get(fam) or [])
        new_arr: list[str] = []
        seen: set[str] = set()
        for key in arr:
            if fam == family:
                k = new_key if key == old_key else key
            elif key in (old_key, new_key):
                continue
            else:
                k = key
            if k not in seen:
                new_arr.append(k)
                seen.add(k)
        if fam == family and new_key not in seen:
            new_arr.append(new_key)
        if new_arr != arr:
            po[fam] = new_arr
            changed = True
    return changed
