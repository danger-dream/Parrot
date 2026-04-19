"""主调度：筛选 → 亲和 → 评分排序。

返回一个按尝试顺序排好的候选列表 [(Channel, resolved_model), ...]。
调用方（failover）顺序尝试，直到成功发首包或全部失败。
"""

from __future__ import annotations

from typing import Optional

from . import affinity, config, cooldown, fingerprint, scorer
from .channel import registry
from .channel.base import Channel


class ScheduleResult:
    """调度结果，包含候选序列与亲和相关元数据。"""

    def __init__(self, candidates: list[tuple[Channel, str]],
                 fp_query: Optional[str], affinity_hit: bool):
        self.candidates = candidates
        self.fp_query = fp_query         # 本次请求计算得到的查询指纹（可用于后续事件记录）
        self.affinity_hit = affinity_hit

    def __bool__(self) -> bool:
        return bool(self.candidates)


# ─── 筛选 ─────────────────────────────────────────────────────────

def _family(proto: str) -> str:
    """协议到家族的映射。跨家族互转不做，scheduler 用这个过滤候选。"""
    return "anthropic" if proto == "anthropic" else "openai"


def _filter_candidates(requested_model: str,
                       ingress_protocol: str = "anthropic") -> list[tuple[Channel, str]]:
    ingress_family = _family(ingress_protocol)
    out: list[tuple[Channel, str]] = []
    for ch in registry.all_channels():
        if not ch.enabled:
            continue
        if ch.disabled_reason:
            continue
        ch_protocol = getattr(ch, "protocol", "anthropic")
        if _family(ch_protocol) != ingress_family:
            continue
        resolved = ch.supports_model(requested_model)
        if resolved is None:
            continue
        if cooldown.is_blocked(ch.key, resolved):
            continue
        out.append((ch, resolved))
    return out


# ─── 亲和匹配 ─────────────────────────────────────────────────────

def _apply_affinity(candidates: list[tuple[Channel, str]],
                    fp_query: Optional[str],
                    cfg: dict) -> tuple[list[tuple[Channel, str]], bool]:
    """尝试把亲和绑定的渠道顶到首位，必要时打破绑定。

    返回 (新 candidates, 是否亲和命中)。
    """
    if not fp_query or len(candidates) <= 1:
        return candidates, False

    bound = affinity.get(fp_query)
    if not bound:
        return candidates, False

    # 在当前候选列表中找到绑定目标
    bound_idx: Optional[int] = None
    for i, (ch, model) in enumerate(candidates):
        if ch.key == bound["channel_key"] and model == bound["model"]:
            bound_idx = i
            break

    if bound_idx is None:
        # 绑定目标当前不在候选（禁用/冷却/删除），保留绑定让下次恢复时命中
        return candidates, False

    # 打破检查：绑定 vs 最优 分数（最优 = 候选集中最低分）
    threshold = float(cfg.get("affinity", {}).get("threshold", 3.0))
    best_score = _best_score(candidates)
    bound_score = scorer.get_score(bound["channel_key"], bound["model"])

    # baseline 兜底：best_score=0 是边缘场景（默认分通常 3000，不会归零）；
    # 给 1.0 的下限避免乘 0 导致永远不打破。
    baseline = max(best_score, 1.0)
    if bound_score > baseline * threshold:
        affinity.delete(fp_query)
        return candidates, False

    # 命中：把绑定目标顶到首位
    if bound_idx != 0:
        candidates = list(candidates)
        candidates.insert(0, candidates.pop(bound_idx))
    affinity.touch(fp_query)
    return candidates, True


def _best_score(candidates: list[tuple[Channel, str]]) -> float:
    """评分越低越好；返回候选集中最低分（最优）。"""
    scores = [scorer.get_score(ch.key, m) for ch, m in candidates]
    return min(scores) if scores else 0.0


# ─── 主入口 ───────────────────────────────────────────────────────

def schedule(body: dict, api_key_name: str, client_ip: str,
             ingress_protocol: str = "anthropic",
             fp_query: Optional[str] = None) -> ScheduleResult:
    """对下游请求做调度，返回候选尝试顺序。

    `ingress_protocol`（anthropic/chat/responses）决定筛候选时的家族过滤。
    `fp_query` 允许调用方提供已算好的亲和查询指纹；未提供时对 anthropic 入口
    按原逻辑用 messages 列表计算；其他入口本版本不算（MS-7 接入）。
    """
    requested_model = body.get("model")
    if not requested_model:
        return ScheduleResult([], None, False)

    candidates = _filter_candidates(requested_model, ingress_protocol)
    if not candidates:
        return ScheduleResult([], None, False)

    if fp_query is None and ingress_protocol == "anthropic":
        fp_query = fingerprint.fingerprint_query(
            api_key_name, client_ip, body.get("messages") or []
        )

    cfg = config.get()
    mode = (cfg.get("channelSelection") or "smart").lower()

    if mode == "smart":
        candidates = scorer.sort_by_score(candidates)
    # "order" 模式：按注册表原始顺序（config 中定义的顺序）

    candidates, affinity_hit = _apply_affinity(candidates, fp_query, cfg)

    return ScheduleResult(candidates, fp_query, affinity_hit)
