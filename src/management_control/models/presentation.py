"""Model-center facts Telegram shows that ModelView does not carry (round 3).

Mirrors ``telegram/menus/model_center_catalog.py`` / ``model_center_sources``
/ ``model_center_usage`` as structured data:

* list: effective state text code, WorkBuddy / Qoder credit rates, lifetime
  usage (累计用量 / 请求 / 成功率 / TPS) and the shared capacity/price values;
* detail: scheduling mode and order, per-source rank / scheduled flag /
  not-scheduled reason, sources sorted like TG (scheduled first in order,
  then the rest), and the source account's quota facts.

Everything is read from cached state; lifetime usage uses the shared
non-blocking statistics cache (``usageLoading`` = TG 「累计统计加载中」).
"""

from __future__ import annotations

from typing import Any, Mapping

from .common import ModelKind, ModelSourceType

META_KEYS = (
    "contextWindow", "maxInputTokens", "maxOutputTokens", "compactTriggerTokens",
    "vision", "toolCall", "structuredOutput", "reasoningEfforts", "serviceTiers", "knowledgeCutoff",
)
_COST_PATHS = {
    "input": (("cost", "input"), ("inputPricePer1M",), ("cost", "inputPrice")),
    "output": (("cost", "output"), ("outputPricePer1M",), ("cost", "outputPrice")),
    "cacheRead": (("cost", "cache_read"), ("cacheReadPricePer1M",), ("cost", "cacheRead")),
    "cacheWrite": (("cost", "cache_write"), ("cacheWritePricePer1M",), ("cost", "cacheWrite")),
    "longContextInput": (("cost", "context_over_200k", "input"), ("longContextInputPricePer1M",),
                         ("cost", "longContextInput"), ("cost", "long_input")),
    "longContextOutput": (("cost", "context_over_200k", "output"), ("longContextOutputPricePer1M",),
                          ("cost", "longContextOutput"), ("cost", "long_output")),
}


def _path(metadata: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = metadata
    for part in path:
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def display_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """The TG 容量 / 能力 / 价格 fields with known values only (TG ``_fields``)."""
    metadata = metadata if isinstance(metadata, Mapping) else {}
    result: dict[str, Any] = {}
    for key in META_KEYS:
        value = metadata.get(key)
        if value is None or value == "" or value == () or value == []:
            continue
        result[key] = list(value) if isinstance(value, tuple) else value
        if key == "contextWindow":
            try:
                maximum, normal = int(metadata.get("contextWindowMaxMode") or 0), int(value or 0)
            except (TypeError, ValueError):
                maximum = normal = 0
            if maximum > normal > 0:
                result["contextWindowMaxMode"] = maximum
    cost = {}
    for name, paths in _COST_PATHS.items():
        for path in paths:
            value = _path(metadata, path)
            if value is not None and value != "":
                cost[name] = value
                break
    if cost:
        result["cost"] = cost
    return result


def shared_metadata(sources) -> dict[str, Any]:
    """TG: values identical across scheduled sources (else all sources)."""
    entries = [(item, display_metadata(item.effective_metadata)) for item in sources]
    live = [entry for entry in entries if entry[0].effective_routable]
    basis = [entry for entry in live or entries if entry[1]] or live or entries
    if not basis:
        return {}
    first = basis[0][1]
    shared = {key: value for key, value in first.items() if key != "cost"
              and all(entry[1].get(key) == value for entry in basis[1:])}
    cost = {key: value for key, value in (first.get("cost") or {}).items()
            if all((entry[1].get("cost") or {}).get(key) == value for entry in basis[1:])}
    if cost:
        shared["cost"] = cost
    return shared


def list_state(view) -> str | None:
    """TG ``_list_model_state`` (no source filter) as a code; None = available."""
    if view.identity.kind not in (ModelKind.CHAT, ModelKind.IMAGE):
        return "configured"
    if view.available_in(None):
        return None if view.global_enabled else "sourceOverride"
    if not view.sources:
        return "noSources"
    if not view.global_enabled and not any(row.enabled_override for row in view.sources):
        return "globalDisabled"
    if all(not row.container_enabled for row in view.sources):
        kinds = {row.type for row in view.sources}
        return ("accountDisabled" if kinds == {ModelSourceType.OAUTH}
                else "channelDisabled" if kinds == {ModelSourceType.API} else "sourceContainerDisabled")
    if all(not row.source_enabled for row in view.sources):
        return "sourceDisabled"
    return "unavailable"


def not_scheduled_reason(view, item) -> str | None:
    """TG ``_not_scheduled_reason`` as a code."""
    if item.effective_routable:
        return None
    if item.unavailable_reason == "account_restricted":
        return "accountRestricted"
    if not item.container_enabled:
        return "containerDisabled"
    if not item.source_enabled:
        return "sourceDisabled"
    if not view.global_enabled and not item.enabled_override:
        return "globalDisabled"
    return "unavailable"


def credit_rates(view) -> dict[str, list[str]]:
    """TG 💳 WorkBuddy 倍率 / Qoder 积分倍率 lines (distinct values)."""
    from src.oauth.qoder.catalog import credit_label
    from src.oauth.workbuddy.catalog import credit_multiplier

    rates: dict[str, list[str]] = {}
    for item in view.sources:
        if item.provider == "workbuddy":
            value = credit_multiplier(item.effective_metadata.get("workbuddyCreditMultiplier"))
            text = "unknown" if value is None else str(value)  # multiplier; "0" = free
            rates.setdefault("workbuddy", [])
            if text not in rates["workbuddy"]:
                rates["workbuddy"].append(text)
        elif item.provider == "qoder":
            label = credit_label(item.effective_metadata)
            if label:
                rates.setdefault("qoder", [])
                if label not in rates["qoder"]:
                    rates["qoder"].append(label)
    return rates


def source_key(item) -> str:
    if item.type is ModelSourceType.OAUTH:
        from src.management_control.routing_account_ids import oauth_channel_key_from_account_id
        return oauth_channel_key_from_account_id(item.id)
    return str(item.id)


def schedule(view, lb) -> dict[str, Any]:
    """TG ``_schedule``: mode, whether this model has its own order, the order."""
    mode = lb.get_mode()
    keys = {source_key(item) for item in view.sources}
    if mode == "priority":
        channels = [channel for channel in lb.channels_for_model(view.model_id) if channel.key in keys]
        return {"mode": mode, "own_order": bool(lb.has_model_priority(view.model_id)),
                "order": list(lb.effective_order_for_model(view.model_id, channels))}
    if mode == "order":
        return {"mode": mode, "own_order": False,
                "order": [channel.key for channel in lb.all_channels() if channel.key in keys]}
    return {"mode": mode, "own_order": False, "order": []}


def ordered_sources(view, order: list[str]) -> list[tuple[Any, int | None, int]]:
    """``(source, scheduleRank, displayOrder)`` sorted like TG: scheduled sources
    by their scheduling position, then the sources out of scheduling."""
    rank = {key: position for position, key in enumerate(order, 1)}
    indexed = list(enumerate(view.sources))
    if order:
        indexed.sort(key=lambda pair: rank.get(source_key(pair[1]), len(rank) + 1))
    live = [pair for pair in indexed if pair[1].effective_routable]
    rest = [pair for pair in indexed if not pair[1].effective_routable]
    numbers = iter(range(1, len(live) + 1))
    result = []
    for index, (_original, item) in enumerate(live + rest):
        scheduled_rank = (next(numbers) if order and source_key(item) in rank else None) if item.effective_routable else None
        result.append((item, scheduled_rank, index))
    return result


def lifetime_usage(views, stats_control, context, source=None) -> tuple[dict[str, dict], bool]:
    """TG model_center_usage: lifetime 累计用量 per model (non-blocking)."""
    from src.management_control.stats_cache import STATS_CACHE

    selections: dict[str, tuple] = {}
    for view in views:
        routes = set(selections.get(view.model_id, ()))
        for item in view.sources:
            if item.type in (ModelSourceType.OAUTH, ModelSourceType.API):
                routes.add((source_key(item), item.outbound_model))
        selections[view.model_id] = tuple(routes)
    snapshot, loading = STATS_CACHE.peek_or_refresh(
        ("model-center-usage",), lambda: stats_control.model_center_usage_snapshot(context),
    )
    if snapshot is None:
        return {}, loading
    channel = None
    if source is not None and source.type in (ModelSourceType.OAUTH, ModelSourceType.API):
        channel = source_key(source)
        selections = {model: tuple(route for route in routes if route[0] == channel)
                      for model, routes in selections.items()}
    projected = stats_control.project_model_center_usage(snapshot, selections, channel_key=channel)
    usage = {}
    for model_id, raw in projected.items():
        if not raw or not (raw.get("total") or any(raw.get(k) for k in ("input", "output", "cache_creation", "cache_read"))):
            continue
        number = lambda key: int(raw.get(key) or 0)
        prompt = number("input") + number("cache_creation") + number("cache_read")
        usage[model_id] = {
            "request_count": number("total"), "success_count": number("success_count"),
            "error_count": number("error_count"), "input_tokens": number("input"),
            "output_tokens": number("output"), "cache_creation_tokens": number("cache_creation"),
            "cache_read_tokens": number("cache_read"), "prompt_tokens": prompt,
            "cache_read_ratio": number("cache_read") / prompt if prompt else None,
            "success_rate": number("success_count") / number("total") if number("total") else None,
            "average_tokens_per_second": raw.get("avg_tps"),
            "maximum_tokens_per_second": raw.get("max_tps"),
            "minimum_tokens_per_second": raw.get("min_tps"),
        }
    return usage, loading


def oauth_source_facts(oauth_control, account_ids: set[str]) -> dict[str, dict]:
    """Quota/balance/credits of each OAuth source account (account card, cached only)."""
    facts: dict[str, dict] = {}
    try:
        cooldowns = list(oauth_control.backend.cooldown_entries())
    except Exception:
        cooldowns = []
    for account_id in account_ids:
        try:
            account = oauth_control.backend.get_account_exact(account_id)
            if account is None:
                continue
            provider = oauth_control.backend.provider_of(account)
            summary = oauth_control._summary(account)
            facts[account_id] = {
                **oauth_control._account_card(account_id, provider, account, cooldowns, light=True),
                "plan_type": account.get("plan_type") or None,
                "available": summary.available, "quota_limited": summary.quota_limited,
                "invalid": summary.invalid, "disabled_reason": summary.disabled_reason,
                "disabled_until": summary.disabled_until,
            }
        except Exception:
            continue
    return facts


def primary_balance(usage) -> dict | None:
    """TG ``_usage_summary``: 可用余额, else 总余额, else the first balance."""
    snap = dict(usage.snapshot or {}) if usage is not None else {}
    balances = [item for item in snap.get("balances") or [] if isinstance(item, dict)]
    if not balances:
        return None
    pick = lambda ident, label: next((item for item in balances
                                      if item.get("id") == ident or item.get("label") == label), None)
    primary = pick("available", "可用余额") or pick("total", "总余额") or pick("key_remaining", "Key 剩余额度") or balances[0]
    return {"label": primary.get("label"), "amount": primary.get("value"),
            "currency": primary.get("currency"), "fetched_at": usage.fetched_at}


def api_source_facts(channel_control, context, channel_ids: set[str]) -> dict[str, dict]:
    """Provider balance of each API source channel (cached provider usage only)."""
    facts: dict[str, dict] = {}
    for channel_id in channel_ids:
        try:
            view = channel_control.get_channel(context, channel_id)
        except Exception:
            continue
        usage = view.provider_usage
        facts[channel_id] = {
            "balance": primary_balance(usage),
            "provider_usage": {
                "status": usage.status, "supported": usage.supported, "stale": usage.stale,
                "fetched_at": usage.fetched_at, "snapshot": dict(usage.snapshot or {}) or None,
            },
            "health": getattr(view.health, "value", view.health),
            "cooldown_count": view.cooldown_count,
        }
    return facts


def present(control, context, views, *, detail: bool, source=None) -> dict[str, dict]:
    """Per resource key: the extra TG facts for listModels / getModel."""
    from src.management_control.load_balancing import load_balancing_control
    from src.management_control.observability.stats import DEFAULT_STATS_CONTROL

    usage, loading = lifetime_usage(views, DEFAULT_STATS_CONTROL, context, source)
    result: dict[str, dict] = {}
    for view in views:
        entry: dict[str, Any] = {
            "state_code": list_state(view),
            "credit_rates": credit_rates(view),
            "shared_metadata": shared_metadata(view.sources),
            "usage": usage.get(view.model_id),
            "usage_loading": loading and view.model_id not in usage,
        }
        if detail:
            plan = (schedule(view, load_balancing_control) if view.identity.kind is ModelKind.CHAT
                    else {"mode": None, "own_order": False, "order": []})
            ordered = ordered_sources(view, plan["order"])
            accounts = oauth_source_facts(control.oauth, {item.id for item in view.sources
                                                          if item.type is ModelSourceType.OAUTH})
            channels = api_source_facts(control.channels, context, {item.id for item in view.sources
                                                                    if item.type is ModelSourceType.API})
            entry["schedule"] = plan
            entry["sources"] = [{
                "source": item, "schedule_rank": rank, "display_order": order,
                "not_scheduled_reason": not_scheduled_reason(view, item),
                "account": accounts.get(item.id) if item.type is ModelSourceType.OAUTH else None,
                "channel": channels.get(item.id) if item.type is ModelSourceType.API else None,
            } for item, rank, order in ordered]
        result[view.resource_key] = entry
    return result
