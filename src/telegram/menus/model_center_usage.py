"""Model-center lifetime usage: shared SWR snapshot and SQL-free projections.

The menu owns page tokens/loading UI. This adapter never starts a worker or
queries SQLite on the caller; DETAIL_STATS uses the existing coordinator.
"""
from __future__ import annotations

from ...management_control.routing_account_ids import oauth_channel_key_from_account_id
from .. import menu_cache, ui


_KEY = ("model_center_usage", "retained_lifetime", 1)


def _channel(source):
    kind = str(getattr(source.type, "value", source.type))
    if kind == "oauth":
        return oauth_channel_key_from_account_id(source.id)
    if kind == "api":
        return str(source.id)
    return None


def _selection(views, source):
    selections = {}
    channel = _channel(source) if source is not None else None
    for view in views:
        routes = set(selections.get(view.model_id, ()))
        for item in view.sources:
            key = _channel(item)
            if key is not None and (channel is None or key == channel):
                routes.add((key, item.outbound_model))
        selections[view.model_id] = tuple(routes)
    return selections, channel


def _project(value, selection):
    if value is None:
        return None
    selections, channel = selection
    return menu_cache._STATS_CONTROL.project_model_center_usage(value, selections, channel_key=channel)


def _read(read, selection):
    return menu_cache.CacheRead(
        _project(read.value, selection), read.fresh, read.refreshing, read.error,
    )


def peek(views, source=None):
    """Return a cheap current-page projection; never schedules or performs SQL."""
    return _read(menu_cache.DETAIL_STATS.peek(_KEY), _selection(views, source))


def request(views, source=None, *, subscriber=None, on_ready=None):
    """Share one lifetime load across all pages, source filters and subscribers.

    on_ready follows SWRCache: a failed load yields (None, error); peek retains
    the previous successful snapshot. The captured projection performs no SQL.
    """
    selection = _selection(views, source)
    callback = None
    if on_ready is not None:
        def callback(value, error):
            on_ready(_project(value, selection), error)
    read = menu_cache.DETAIL_STATS.request(
        _KEY, lambda: menu_cache._STATS_CONTROL.model_center_usage_snapshot(menu_cache._CONTEXT),
        subscriber=subscriber, on_ready=callback, interactive=True,
    )
    return _read(read, selection)


def format_lines(metrics):
    """Only the requested three lines; unmeasured TPS is omitted, not zeroed."""
    if not metrics or not (metrics.get("total") or any(metrics.get(k) for k in (
        "input", "output", "cache_creation", "cache_read",
    ))):
        return []
    prompt = ui.prompt_total(metrics.get("input"), metrics.get("cache_creation"), metrics.get("cache_read"))
    lines = [
        f"💎 累计用量: ↑ {ui.fmt_tokens(prompt)} · ↓ {ui.fmt_tokens(metrics.get('output'))} · "
        + ui.fmt_cache_phrase(metrics.get("cache_read"), prompt),
        f"📨 请求：{int(metrics.get('total') or 0)} 次 · 成功率 "
        + ui.fmt_rate(metrics.get("success_count"), metrics.get("total"))
        + f" · 失败 {int(metrics.get('error_count') or 0)} 次",
    ]
    if metrics.get("avg_tps") is not None:
        lines.append(
            f"⚡️ TPS: 平均 {ui.fmt_tps(metrics.get('avg_tps'))} · "
            f"峰值 {ui.fmt_tps(metrics.get('max_tps'))} · 最低 {ui.fmt_tps(metrics.get('min_tps'))}"
        )
    return lines
