"""API-key list facts Telegram shows beyond ApiKeyView (round 3).

* ``history_totals``: lifetime request count per key (TG 「闲置（历史 N 次）」),
  read through the non-blocking statistics cache.
* ``list_summary``: TG list header (共 / 活跃 / 停用 / 闲置 / 本月次数 / ↑↓).
"""

from __future__ import annotations

from typing import Any

from src import log_db
from src.management_auth import AuthMethod


def history_totals(context, statistics: Any = log_db) -> tuple[dict[str, int], bool]:
    from src.management_control.stats_cache import STATS_CACHE

    if context is not None and context.actor.auth_method is AuthMethod.TELEGRAM_ADMIN:
        return {}, False
    try:
        if statistics is not log_db:
            return dict(statistics.request_totals_by_apikey() or {}), False
        value, loading = STATS_CACHE.peek_or_refresh(("apikey-history",), statistics.request_totals_by_apikey)
    except Exception:
        return {}, True  # history is decoration; never fail the list
    return dict(value or {}), loading


def list_summary(views, *, all_views=None) -> dict[str, int]:
    rows = list(all_views if all_views is not None else views)
    month = [view.month_stats for view in rows]
    active = sum(1 for usage in month if usage.total > 0)
    disabled = sum(1 for view in rows if not view.enabled)
    return {
        "total": len(rows),
        "active": active,
        "disabled": disabled,
        "idle": len(rows) - active,
        "month_requests": sum(usage.total for usage in month),
        "month_prompt_tokens": sum(usage.input_tokens + usage.cache_creation_tokens + usage.cache_read_tokens
                                   for usage in month),
        "month_output_tokens": sum(usage.output_tokens for usage in month),
    }


def all_keys_summary(context, control) -> dict[str, int]:
    """Header over every key without a second paged list call (filters/pages)."""
    from types import SimpleNamespace

    from src.management_control.channels.list_stats import month_start
    from src.management_control.stats_cache import CachingStatsSource

    try:
        snapshot = CachingStatsSource(control.statistics_source).stats_period_snapshot(month_start())
        by_key = dict((snapshot or {}).get("by_apikey") or {})
    except Exception:
        by_key = {}
    rows = []
    for view in control.snapshot_api_keys(context):
        raw = by_key.get(view.key_id) or {}
        number = lambda key: int(raw.get(key) or 0)
        rows.append(SimpleNamespace(enabled=view.enabled, month_stats=SimpleNamespace(
            total=number("total"), input_tokens=number("input"), cache_creation_tokens=number("cache_creation"),
            cache_read_tokens=number("cache_read"), output_tokens=number("output"))))
    return list_summary(rows)
