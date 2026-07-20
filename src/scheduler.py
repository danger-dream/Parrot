"""主调度：筛选 → 亲和 → 评分排序。

返回一个按尝试顺序排好的候选列表 [(Channel, resolved_model), ...]。
调用方（failover）顺序尝试，直到成功发首包或全部失败。
"""

from __future__ import annotations

from typing import Optional

from . import affinity, concurrency, config, cooldown, fingerprint, load_balancing, scorer
from .channel import registry
from .channel.base import Channel
from .protocols.matrix import (
    DEFAULT_MATRIX,
    ProtocolGuardError,
    RoutePlan,
    capabilities_for_channel,
    extract_request_features,
)


class ScheduleResult:
    """调度结果，包含候选序列与亲和相关元数据。"""

    def __init__(self, candidates: list[tuple[Channel, str]],
                 fp_query: Optional[str], affinity_hit: bool,
                 client_key: Optional[str] = None,
                 saturated: Optional[list[tuple[Channel, str]]] = None,
                 route_plans: Optional[dict[tuple[str, str], RoutePlan]] = None,
                 guard_errors: Optional[list[str]] = None,
                 exclusions: Optional[list[dict]] = None):
        self.candidates = candidates
        self.fp_query = fp_query         # 本次请求计算得到的查询指纹（可用于后续事件记录）
        self.affinity_hit = affinity_hit
        self.client_key = client_key     # client-level affinity key（failover 写入用）
        # 并发饱和（in_flight >= max）的候选：正常 candidates 全部失败后，
        # failover 会把 saturated 作为"可排队等位"的备选集。
        self.saturated: list[tuple[Channel, str]] = saturated or []
        self.route_plans: dict[tuple[str, str], RoutePlan] = route_plans or {}
        self.guard_errors: list[str] = guard_errors or []
        self.guard_error: Optional[str] = self.guard_errors[0] if self.guard_errors else None
        # 仅供内部日志/告警使用，不回显给 API 客户端。每项代表一个原本支持
        # requested_model、但在调度筛选中被排除的渠道。
        self.exclusions: list[dict] = exclusions or []

    def exclusion_summary(self, limit: int = 12) -> str:
        parts: list[str] = []
        for item in self.exclusions[:max(0, int(limit))]:
            channel = str(item.get("channel") or "?")
            reason = str(item.get("reason") or "unknown")
            detail = str(item.get("detail") or "").strip()
            parts.append(f"{channel}={reason}{f'({detail})' if detail else ''}")
        remaining = len(self.exclusions) - len(parts)
        if remaining > 0:
            parts.append(f"+{remaining} more")
        return "; ".join(parts) or "no model-supporting channels"

    def route_plan_for(self, ch: Channel, model: str) -> Optional[RoutePlan]:
        return self.route_plans.get((getattr(ch, "key", ""), model))

    def __bool__(self) -> bool:
        return bool(self.candidates) or bool(self.saturated)


# ─── 筛选 ─────────────────────────────────────────────────────────

def _filter_candidates(requested_model: str,
                       ingress_protocol: str = "anthropic",
                       body: Optional[dict] = None,
                       diagnostics: Optional[list[dict]] = None,
                       ) -> tuple[list[tuple[Channel, str]], list[tuple[Channel, str]], dict[tuple[str, str], RoutePlan], list[str]]:
    """返回 (available, saturated, route_plans)：
       available = 可立即尝试的候选；
       saturated = 其它条件 OK 但当前并发满的候选（作为排队备选）。
    """
    features = extract_request_features(ingress_protocol, body or {})
    available: list[tuple[Channel, str]] = []
    saturated: list[tuple[Channel, str]] = []
    route_plans: dict[tuple[str, str], RoutePlan] = {}
    guard_errors: list[str] = []
    excluded = diagnostics if diagnostics is not None else []
    for ch in registry.all_channels():
        resolved = ch.supports_model(requested_model)
        if resolved is None:
            continue
        if not ch.enabled:
            excluded.append({"channel": ch.key, "reason": "disabled"})
            continue
        if ch.disabled_reason:
            excluded.append({
                "channel": ch.key,
                "reason": "disabled_reason",
                "detail": str(ch.disabled_reason)[:240],
            })
            continue
        ch_protocol = getattr(ch, "protocol", "anthropic")
        try:
            route_plan = DEFAULT_MATRIX.plan(
                ingress_protocol,
                ch_protocol,
                features=features,
                capabilities=capabilities_for_channel(ch),
            )
        except ProtocolGuardError as exc:
            guard_errors.append(exc.reason)
            excluded.append({
                "channel": ch.key,
                "reason": "protocol_guard",
                "detail": str(exc.reason)[:240],
            })
            continue
        route_plans[(ch.key, resolved)] = route_plan
        if cooldown.is_blocked(ch.key, resolved):
            state = cooldown.get_state(ch.key, resolved) or {}
            until = state.get("cooldown_until")
            detail = "permanent" if until == -1 else f"until={until}"
            excluded.append({
                "channel": ch.key,
                "reason": "cooldown",
                "detail": detail,
            })
            continue
        if concurrency.is_saturated(ch.key):
            excluded.append({"channel": ch.key, "reason": "saturated"})
            saturated.append((ch, resolved))
            continue
        available.append((ch, resolved))
    return available, saturated, route_plans, guard_errors


# ─── 亲和匹配 ─────────────────────────────────────────────────────

def _apply_affinity(candidates: list[tuple[Channel, str]],
                    fp_query: Optional[str],
                    client_key: Optional[str] = None,
                    ) -> tuple[list[tuple[Channel, str]], bool]:
    """尝试把亲和绑定的渠道顶到首位。

    优先使用 fingerprint 亲和（精确会话级别）。
    若 fp_query 为 None 或未命中，回退到 client-level soft affinity。

    返回 (新 candidates, 是否亲和命中)。
    """
    if len(candidates) <= 1:
        return candidates, False

    # 1. 尝试 fingerprint 亲和
    bound = affinity.get(fp_query) if fp_query else None
    source = "fp"  # 记录命中来源

    # 2. 回退到 client-level soft affinity
    if not bound and client_key:
        bound = affinity.client_get(client_key)
        source = "client"

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

    # 命中：只要绑定目标当前仍在候选列表里，就把它顶到首位。
    # 负载均衡算法负责“亲和不可用时选谁接班”；亲和负责“原渠道还能用就继续用”。
    if bound_idx != 0:
        candidates = list(candidates)
        candidates.insert(0, candidates.pop(bound_idx))
    if source == "fp" and fp_query:
        affinity.touch(fp_query)
    return candidates, True


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

    exclusions: list[dict] = []
    candidates, saturated, route_plans, guard_errors = _filter_candidates(
        requested_model,
        ingress_protocol,
        body=body,
        diagnostics=exclusions,
    )
    if not candidates and not saturated:
        return ScheduleResult(
            [], None, False,
            guard_errors=guard_errors,
            exclusions=exclusions,
        )

    if fp_query is None and ingress_protocol == "anthropic":
        fp_query = fingerprint.fingerprint_query(
            api_key_name, client_ip, body.get("messages") or []
        )

    # 构造 client-level affinity key
    client_key = affinity.make_client_key(api_key_name, client_ip, requested_model)

    cfg = config.get()
    mode = (cfg.get("channelSelection") or "smart").lower()

    if mode == "smart":
        candidates = scorer.sort_by_score(candidates)
        saturated = scorer.sort_by_score(saturated)
    elif mode == "priority":
        candidates = load_balancing.sort_candidates_by_priority(candidates, cfg)
        saturated = load_balancing.sort_candidates_by_priority(saturated, cfg)
    # "order" 模式：按注册表原始顺序（config 中定义的顺序）

    candidates, affinity_hit = _apply_affinity(candidates, fp_query,
                                               client_key=client_key)

    return ScheduleResult(candidates, fp_query, affinity_hit,
                          client_key=client_key, saturated=saturated,
                          route_plans=route_plans, guard_errors=guard_errors,
                          exclusions=exclusions)
