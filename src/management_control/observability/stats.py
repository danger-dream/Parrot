"""Statistics queries and Telegram statistics preferences."""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from src import concurrency as concurrency_module
from src import config as config_module
from src import log_db as log_db_module
from src import log_timeseries as log_timeseries_module
from src import model_names
from src import oauth_manager as oauth_manager_module
from src import state_db as state_db_module
from src.channel import registry as registry_module
from src.management_auth import Capability
from src.management_control.context import AuditSink, ManagementContext, audit_record
from src.management_control.errors import ManagementError, ManagementErrorCode

from src.management_control.stats_cache import cached, snapshot_key

from ._log_errors import map_historical_log_errors
from .stats_view import summary_tops, with_rates
from .common import PageResult, page_slice, require, revision_for
from .compare import HourCache, build_compare
from .timeseries import TimeseriesBucket, TimeseriesGroup, TimeseriesRange, build_timeseries


_BJT = timezone(timedelta(hours=8))
_PREF_KEYS = ("byChannel", "byModel", "byApiKey", "cacheMisses", "recentCalls")


class StatsPeriod(str, Enum):
    TODAY = "today"
    THREE_DAYS = "3d"
    SEVEN_DAYS = "7d"
    MONTH = "month"
    LIFETIME = "lifetime"


class StatsDimension(str, Enum):
    CHANNEL = "channel"
    MODEL = "model"
    API_KEY = "apiKey"


class StatsSort(str, Enum):
    TOTAL = "total"
    SUCCESS = "success"
    TOKENS = "tokens"
    COST = "cost"
    LATENCY = "latency"
    TPS = "tps"
    NAME = "name"


@dataclass(frozen=True, slots=True)
class StatsBreakdownQuery:
    dimension: StatsDimension
    period: StatsPeriod
    sort: StatsSort = StatsSort.TOTAL
    descending: bool = True
    page: int = 1
    page_size: int = 50
    # Round 3: TG family Top blocks / one account's model breakdown.
    family: str | None = None
    channel: str | None = None


class StatsControl:
    def __init__(
        self,
        *,
        log_db=log_db_module,
        config=config_module,
        concurrency=concurrency_module,
        registry=registry_module,
        oauth_manager=oauth_manager_module,
        state_db=state_db_module,
        audit_sink: AuditSink | None = None,
        now=time.time,
        timeseries_source=log_timeseries_module,
    ) -> None:
        self.log_db = log_db
        self.timeseries_source = timeseries_source
        self.config = config
        self.concurrency = concurrency
        self.registry = registry
        self.oauth_manager = oauth_manager
        self.state_db = state_db
        self.audit_sink = audit_sink
        self._now = now
        self._compare_hours = HourCache()

    def _audit(self, context: ManagementContext, action: str, result: str) -> None:
        if self.audit_sink is not None:
            self.audit_sink.record(audit_record(
                context, action=action, target="preferences.telegram.stats", result=result,
            ))

    def period_since(self, period: StatsPeriod | str) -> float:
        period = StatsPeriod(period)
        now = datetime.fromtimestamp(self._now(), tz=_BJT)
        if period is StatsPeriod.TODAY:
            return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        if period is StatsPeriod.MONTH:
            return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        if period is StatsPeriod.THREE_DAYS:
            return self._now() - 3 * 86400
        if period is StatsPeriod.SEVEN_DAYS:
            return self._now() - 7 * 86400
        return 0.0

    @staticmethod
    def telegram_period(period: str) -> StatsPeriod:
        return {
            "0": StatsPeriod.TODAY,
            "3": StatsPeriod.THREE_DAYS,
            "7": StatsPeriod.SEVEN_DAYS,
            "month": StatsPeriod.MONTH,
        }.get(str(period), StatsPeriod.THREE_DAYS)

    def period_snapshot(self, context: ManagementContext, period: StatsPeriod | str) -> dict[str, Any]:
        require(context)
        normalized = StatsPeriod(period)
        # API lifetime breakdown needs the same complete dimensional snapshot;
        # the lighter stats_lifetime() remains available to the TG cache worker.
        # Cached per period (stale-while-revalidate, see stats_cache); the
        # aggregation itself and therefore every statistic is unchanged.
        since = self.period_since(normalized)
        raw = cached(
            snapshot_key(normalized.value, since),
            lambda: self.log_db.stats_period_snapshot(since),
            source=self.log_db,
        )
        return copy.deepcopy(raw or {})

    def warm_cache(self) -> None:
        """Precompute the month snapshot shared by statistics and API-key pages.

        Called once from a background thread after startup; a no-op when the
        statistics cache is disabled. Failures only leave the cache cold.
        """
        from src.management_control.stats_cache import STATS_CACHE
        if not STATS_CACHE.enabled or self.log_db is not log_db_module:
            return
        try:
            since = self.period_since(StatsPeriod.MONTH)
            cached(snapshot_key("month", since), lambda: self.log_db.stats_period_snapshot(since),
                   source=self.log_db)
            # Round 3: API-key 「历史 N 次」 and model-center lifetime usage.
            cached(("apikey-history",), self.log_db.request_totals_by_apikey, source=self.log_db)
            cached(("model-center-usage",), lambda: self._model_center_usage_raw(), source=self.log_db)
            # Closed hours of this and last month, so period comparisons (any filter) start warm.
            build_compare(self.timeseries_source, now=self._now(), period="month", hours=self._compare_hours)
        except Exception as exc:
            print(f"[management] statistics warm-up skipped ({type(exc).__name__})")

    def period_snapshot_since(self, context: ManagementContext, since_ts: float) -> dict[str, Any]:
        """Compatibility query for TG's exact BJT cache keys."""
        require(context)
        return copy.deepcopy(self.log_db.stats_period_snapshot(float(since_ts)) or {})

    def lifetime_snapshot(self, context: ManagementContext) -> dict[str, Any]:
        require(context)
        return copy.deepcopy(self.log_db.stats_lifetime() or {})

    def summary(self, context: ManagementContext, period: StatsPeriod | str) -> dict[str, Any]:
        snapshot = self.period_snapshot(context, period)
        summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else snapshot
        overall = copy.deepcopy(summary.get("overall") or {})
        raw_families = snapshot.get("families") or summary.get("families") or summary.get("by_family") or {}
        families = {
            str(name): copy.deepcopy((value.get("overall") if isinstance(value, dict) else {}) or {})
            for name, value in raw_families.items()
        } if isinstance(raw_families, dict) else {}
        data = {
            "period": StatsPeriod(period).value,
            "overall": self._normalize_metrics(overall),
            "families": {
                name: self._normalize_metrics(metrics) for name, metrics in families.items()
            },
            # Round 3: TG 汇总 Top blocks (per family channel/model Top 3 with
            # "所属" channels, cross-family Key Top 3 with family split).
            **summary_tops(snapshot, raw_families, self._normalize_metrics),
        }
        # Hash exactly the non-secret public representation.  Hidden aggregate
        # columns and internal timestamps must not perturb a client revision.
        data["revision"] = revision_for(data)
        return data

    @staticmethod
    def _normalize_metrics(raw: Any) -> dict[str, Any]:
        """Map every authoritative aggregate shape to one stable API metric DTO."""
        return with_rates(StatsControl._base_metrics(raw))

    @staticmethod
    def _base_metrics(raw: Any) -> dict[str, Any]:
        metrics = raw if isinstance(raw, dict) else {}

        def integer(*keys: str) -> int:
            for key in keys:
                if metrics.get(key) is not None:
                    return int(metrics[key] or 0)
            return 0

        def number(*keys: str) -> float | None:
            for key in keys:
                if metrics.get(key) is not None:
                    return float(metrics[key])
            return None

        cache_creation = integer("cache_creation", "total_cache_creation", "cache_creation_tokens")
        cache_read = integer("cache_read", "total_cache_read", "cache_read_tokens")
        if metrics.get("input") is not None:
            input_tokens = integer("input")
        elif metrics.get("input_tokens") is not None:
            input_tokens = integer("input_tokens")
        elif metrics.get("total_input_tokens") is not None:
            input_tokens = integer("total_input_tokens")
        else:
            # Group metrics call this prompt tokens and include both cache kinds.
            input_tokens = max(0, integer("total_prompt_tokens") - cache_creation - cache_read)
        service_tiers = metrics.get("service_tier_counts")
        return {
            "total": integer("total"),
            "successCount": integer("success_count"),
            "errorCount": integer("error_count"),
            "pendingCount": integer("pending_count"),
            "inputTokens": input_tokens,
            "outputTokens": integer("output", "total_output_tokens", "output_tokens"),
            "cacheCreationTokens": cache_creation,
            "cacheReadTokens": cache_read,
            "cacheHitRequests": integer("hit_requests", "success_with_cache_hit"),
            "cacheWriteRequests": integer("write_requests", "success_with_cache_write"),
            "totalRetries": integer("total_retries"),
            "retriedRequests": integer("retried_requests"),
            "affinityHits": integer("affinity_hits"),
            "costTicks": integer("cost_ticks", "cost_usd_ticks"),
            "actualCostTicks": integer("actual_cost_ticks"),
            "estimatedCostTicks": integer("estimated_cost_ticks"),
            "actualCostedSuccess": integer("actual_costed_success"),
            "estimatedCostedSuccess": integer("estimated_costed_success"),
            "costedSuccess": integer("costed_success"),
            "unpricedSuccess": integer("unpriced_success"),
            "averageConnectMilliseconds": number("avg_connect_ms"),
            "averageFirstTokenMilliseconds": number("avg_first_token_ms"),
            "averageTotalMilliseconds": number("avg_total_ms"),
            "averageTokensPerSecond": number("avg_tps"),
            "maximumTokensPerSecond": number("max_tps"),
            "minimumTokensPerSecond": number("min_tps"),
            "serviceTierCounts": {
                str(key): int(value or 0) for key, value in service_tiers.items()
            } if isinstance(service_tiers, dict) else {},
        }

    @staticmethod
    def _metric_value(item: dict[str, Any], sort: StatsSort) -> Any:
        metrics = item["metrics"]
        if sort is StatsSort.NAME:
            return str(item.get("key") or "").casefold()
        if sort is StatsSort.SUCCESS:
            return metrics["successCount"]
        if sort is StatsSort.TOKENS:
            return sum(metrics[key] for key in (
                "inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens",
            ))
        if sort is StatsSort.COST:
            return metrics["costTicks"]
        if sort is StatsSort.LATENCY:
            return (
                metrics["averageTotalMilliseconds"]
                or metrics["averageFirstTokenMilliseconds"]
                or metrics["averageConnectMilliseconds"]
                or 0
            )
        if sort is StatsSort.TPS:
            return metrics["averageTokensPerSecond"] or 0
        return metrics["total"]

    def compare(
        self,
        context: ManagementContext,
        *,
        period: str | None = None,
        start: float | None = None,
        end: float | None = None,
        filters: dict[str, str | None] | None = None,
    ) -> dict[str, Any]:
        """This period against the one before it (same elapsed time), plus a sparkline of this one."""
        require(context)
        f = filters or {}
        # Calendar windows carry their day/month so a rollover never serves the old window.
        local = datetime.fromtimestamp(self._now(), tz=_BJT)
        calendar = local.strftime("%Y-%m-%d") if period == "today" else local.strftime("%Y-%m") if period == "month" else None
        key = ("compare", period, calendar, start, end, f.get("model"), f.get("channel"), f.get("apiKey"))
        return cached(key, lambda: build_compare(
            self.timeseries_source, now=self._now(), period=period, start=start, end=end, filters=f,
            hours=self._compare_hours,
        ), source=self.log_db if self.timeseries_source is log_timeseries_module else None)

    def timeseries(
        self,
        context: ManagementContext,
        *,
        range_value: TimeseriesRange | str,
        bucket: TimeseriesBucket | str | None = None,
        group: TimeseriesGroup | str = TimeseriesGroup.NONE,
        limit: int = 6,
    ) -> dict[str, Any]:
        """Continuous, zero-filled bucketed series (same cost rules as summary)."""
        require(context)
        key = ("timeseries", str(getattr(range_value, "value", range_value)),
               str(getattr(bucket, "value", bucket)), str(getattr(group, "value", group)), int(limit))
        return cached(key, lambda: build_timeseries(
            self.timeseries_source, now=self._now(), range_value=range_value,
            bucket=bucket, group=group, limit=limit,
        ), source=self.log_db if self.timeseries_source is log_timeseries_module else None)

    def recent_outcomes(
        self, context: ManagementContext, *, dimension: StatsDimension | str, limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Last ``limit`` finished outcomes (oldest first) per channel / API key / model, over 7 days."""
        require(context)
        dimension = StatsDimension(dimension)
        group = {"channel": "channel", "apiKey": "apikey", "model": "model"}[dimension.value]
        since = self._now() - 7 * 86400

        def load() -> list[dict[str, Any]]:
            raw = self.timeseries_source.recent_outcomes(since=since, group=group, per_key=int(limit))
            items = []
            for key, outcomes in raw.items():
                statuses = [status for _ts, status in outcomes]
                items.append({
                    "key": key,
                    "outcomes": statuses,
                    "successCount": statuses.count("success"),
                    "errorCount": statuses.count("error"),
                    "lastAt": datetime.fromtimestamp(outcomes[-1][0], tz=timezone.utc) if outcomes else None,
                })
            items.sort(key=lambda item: item["key"])
            return items

        return cached(("recent-outcomes", dimension.value, int(limit)), load,
                      source=self.log_db if self.timeseries_source is log_timeseries_module else None)

    def outcome_timeline(
        self, context: ManagementContext, *, dimension: StatsDimension | str, bucket_minutes: int = 60, buckets: int = 24,
    ) -> dict[str, Any]:
        """Finished requests per key in clock-aligned buckets ending with the current one."""
        require(context)
        dimension = StatsDimension(dimension)
        group = {"channel": "channel", "apiKey": "apikey", "model": "model"}[dimension.value]
        step = int(bucket_minutes) * 60
        origin = (int(self._now()) // step - (int(buckets) - 1)) * step

        def load() -> dict[str, Any]:
            raw = self.timeseries_source.outcome_buckets(origin=origin, bucket_seconds=step, count=int(buckets), group=group)
            items = []
            for key, cells in raw.items():
                row = [cells.get(i) or {"total": 0, "errors": 0, "cancelled": 0, "last": 0.0} for i in range(int(buckets))]
                last = max((cell["last"] for cell in row), default=0.0)
                items.append({
                    "key": key,
                    "buckets": [{"total": int(c["total"]), "errorCount": int(c["errors"]), "cancelledCount": int(c["cancelled"])} for c in row],
                    "total": sum(int(c["total"]) for c in row),
                    "errorCount": sum(int(c["errors"]) for c in row),
                    "cancelledCount": sum(int(c["cancelled"]) for c in row),
                    "lastAt": datetime.fromtimestamp(last, tz=timezone.utc) if last else None,
                })
            items.sort(key=lambda item: item["key"])
            return {"from": datetime.fromtimestamp(origin, tz=timezone.utc), "bucketSeconds": step, "items": items}

        return cached(("outcome-timeline", dimension.value, step, int(buckets), origin), load,
                      source=self.log_db if self.timeseries_source is log_timeseries_module else None)

    def breakdown(self, context: ManagementContext, query: StatsBreakdownQuery) -> PageResult[dict[str, Any]]:
        snapshot = self.period_snapshot(context, query.period)
        key = {
            StatsDimension.CHANNEL: "by_channel",
            StatsDimension.MODEL: "by_model",
            StatsDimension.API_KEY: "by_apikey",
        }[query.dimension]
        summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {}
        if query.family is not None:
            families = snapshot.get("families") or {}
            if query.family not in families:
                raise ManagementError(ManagementErrorCode.VALIDATION_FAILED, "Unknown family")
            summary = families[query.family] if isinstance(families[query.family], dict) else {}
        # Summary group rows preserve latency while the object maps preserve
        # token/TPS/cost. Prefer complete summary rows and support legacy maps.
        raw = summary.get(key)
        if raw is None and query.family is None:
            raw = snapshot.get(key)
        raw = raw or {}
        if query.channel is not None:
            if query.dimension is not StatsDimension.MODEL or query.family is not None:
                raise ManagementError(ManagementErrorCode.VALIDATION_FAILED,
                                      "channel applies to dimension=model without family")
            since = self.period_since(StatsPeriod(query.period))
            from src.management_control.routing_account_ids import oauth_channel_key_from_account_id
            channel_key = (query.channel if query.channel.startswith("api:")
                           else oauth_channel_key_from_account_id(query.channel))  # accountId or oauth:<id>
            with map_historical_log_errors():
                rows = cached(("channel-models", channel_key, since),
                              lambda: self.log_db.channel_model_stats(channel_key, since_ts=since),
                              source=self.log_db)
            raw = [{"key": str(row.get("final_model") or "?"), "metrics": row} for row in rows or []]
        if isinstance(raw, dict):
            items = [
                {"key": str(name), "metrics": self._normalize_metrics(metrics)}
                for name, metrics in raw.items()
            ]
        else:
            items = [
                {
                    "key": str(item.get("key") or ""),
                    "metrics": self._normalize_metrics(item.get("metrics")),
                }
                for item in raw if isinstance(item, dict)
            ]
        items.sort(
            key=lambda item: self._metric_value(item, query.sort),
            reverse=query.descending,
        )
        result = page_slice(items, page=query.page, page_size=query.page_size)
        public_items = []
        model_channels = snapshot.get("model_channels") if isinstance(snapshot.get("model_channels"), dict) else {}
        for item in result.items:
            public = {"key": item["key"], "metrics": item["metrics"]}
            if query.dimension is StatsDimension.MODEL and query.channel is None:
                public["channels"] = [{"key": str(entry.get("key") or "?"), "type": entry.get("type"),
                                       "count": int(entry.get("count") or 0)}
                                      for entry in (model_channels.get(item["key"]) or [])[:5]
                                      if isinstance(entry, dict)]
            public["revision"] = revision_for(public)
            public_items.append(public)
        return PageResult(
            tuple(public_items), result.page, result.page_size, result.total,
        )

    def model_stats(self, context: ManagementContext, model_id: str, period: StatsPeriod | str) -> dict[str, Any]:
        snapshot = self.period_snapshot(context, period)
        by_model = snapshot.get("by_model")
        if by_model is None and isinstance(snapshot.get("summary"), dict):
            by_model = snapshot["summary"].get("by_model")
        by_model = by_model or {}
        if isinstance(by_model, list):
            by_model = {str(item.get("key") or ""): item.get("metrics") or {} for item in by_model if isinstance(item, dict)}
        metrics = by_model.get(model_id) if isinstance(by_model, dict) else None
        if metrics is None:
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        # The snapshot already carries channels_by_requested_model() for the
        # same window; reuse it instead of a second full scan.
        channels = snapshot.get("model_channels")
        if not isinstance(channels, dict):
            channels = self.log_db.channels_by_requested_model(self.period_since(StatsPeriod(period)))
        channel_rows = []
        for row in (channels or {}).get(model_id) or []:
            if not isinstance(row, dict):
                continue
            channel_rows.append({
                "key": str(row.get("key") or ""),
                "count": int(row.get("count") or 0),
                "type": str(row.get("type") or "") or None,
                "upstreamProtocol": str(row.get("upstream_protocol") or "") or None,
            })
        data = {
            "modelId": model_id,
            "period": StatsPeriod(period).value,
            "metrics": self._normalize_metrics(metrics),
            "channels": channel_rows,
        }
        data["revision"] = revision_for(data)
        return data

    def cache_misses(self, context: ManagementContext, period: StatsPeriod | str) -> list[dict[str, Any]]:
        """TG「最近未命中样本」: the latest successful requests without a cache read.

        Read from the (cached) period snapshot; the same 3 rows TG shows.
        """
        snapshot = self.period_snapshot(context, period)
        rows = (snapshot.get("summary") or {}).get("recent_cache_misses") or []
        return [{key: value for key, value in row.items() if key != "response_body"}
                for row in rows if isinstance(row, dict)]

    def recent_calls(self, context: ManagementContext, *, page: int, page_size: int) -> PageResult[dict[str, Any]]:
        require(context)
        with map_historical_log_errors():
            rows, total = self.log_db.management_logs_page(page=page, page_size=page_size)
        return PageResult(tuple(copy.deepcopy(row) for row in rows), page, page_size, total)

    def get_preferences(self, context: ManagementContext) -> dict[str, Any]:
        require(context)
        raw = ((self.config.get().get("telegram") or {}).get("statsVisibility") or {})
        values = {key: bool(raw.get(key, True)) for key in _PREF_KEYS}
        values["revision"] = revision_for(values)
        return values

    def _write_preferences(self, context: ManagementContext, patch: dict[str, bool]) -> None:
        unknown = set(patch) - set(_PREF_KEYS)
        if unknown:
            raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)

        def mutate(cfg: dict) -> None:
            telegram = cfg.setdefault("telegram", {})
            visibility = telegram.setdefault("statsVisibility", {})
            for key, value in patch.items():
                visibility[key] = bool(value)

        self.config.update(mutate)
        self._audit(context, "stats.preferences.update", "succeeded")

    def update_preferences(
        self,
        context: ManagementContext,
        patch: dict[str, bool],
        *,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        require(context, Capability.WRITE)
        current = self.get_preferences(context)
        if expected_revision is not None and expected_revision != current["revision"]:
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        self._write_preferences(context, patch)
        return self.get_preferences(context)

    def update_preferences_direct(self, context: ManagementContext, patch: dict[str, bool]) -> None:
        """TG compatibility write without a post-commit DTO read-back."""
        require(context, Capability.WRITE)
        self._write_preferences(context, patch)

    def concurrency_enabled(self, context: ManagementContext) -> bool:
        require(context)
        return bool((self.config.get().get("concurrency") or {}).get("enabled", True))

    def concurrency_totals(self, context: ManagementContext) -> dict[str, Any]:
        require(context)
        return copy.deepcopy(self.concurrency.totals())

    def channel_family(self, context: ManagementContext, channel_key: str) -> str | None:
        require(context)
        channel = self.registry.get_channel(channel_key)
        return str(getattr(channel, "protocol", "") or "") or None

    def oauth_accounts(self, context: ManagementContext) -> list[dict]:
        require(context)
        return copy.deepcopy(self.oauth_manager.list_accounts())

    def quota_row(self, context: ManagementContext, account_key: str) -> dict:
        require(context)
        try:
            return copy.deepcopy(self.state_db.quota_load(account_key) or {})
        except RuntimeError:
            return {}

    def configured_channel_keys(self, context: ManagementContext) -> list[str]:
        require(context)
        keys = []
        for channel in self.config.get().get("channels") or []:
            if isinstance(channel, dict) and str(channel.get("name") or "").strip():
                keys.append("api:" + str(channel["name"]).strip())
        return keys

    def configured_api_key_names(self, context: ManagementContext) -> list[str]:
        require(context)
        values = self.config.get().get("apiKeys") or {}
        return [str(name) for name in values] if isinstance(values, dict) else []

    def request_totals_by_apikey(self, context: ManagementContext) -> dict[str, int]:
        require(context)
        return copy.deepcopy(self.log_db.request_totals_by_apikey())

    def model_center_usage_snapshot(self, context: ManagementContext) -> dict:
        """Internal TG snapshot; raw weights, no API DTO or alias-history guesses."""
        require(context)
        return self._model_center_usage_raw()

    def _model_center_usage_raw(self) -> dict:
        routes = self.log_db.model_center_usage_snapshot()
        by_model: dict[str, dict] = {}
        for (channel, model), raw in routes.items():
            # Cursor/WorkBuddy generic IDs are provider-owned, not global auto/default.
            public_model = model_names.for_channel(channel, model)
            by_model.setdefault(public_model, {})[(channel, model)] = raw
        return {"routes": routes, "by_model": by_model}

    def project_model_center_usage(
        self, snapshot: dict, selections: dict[str, tuple[tuple[str, str], ...]],
        *, channel_key: str | None = None,
    ) -> dict[str, dict]:
        """Pure projection, safe in a menu callback. Never queries logs/config.

        Explicit source/outbound bindings take precedence over a coincidentally
        equal public ID. Other historical sources match exact execution IDs.
        A set of route keys prevents aliases/duplicate catalog entries counting
        the same bucket twice. Current global request aliases are never expanded.
        Multiple public IDs for one route expose the same execution totals, not
        invented per-alias history; these view rows must not be summed together.
        """
        result = {}
        routes = snapshot.get("routes", {})
        by_model = snapshot.get("by_model", {})
        for model_id, bound_routes in selections.items():
            bound_channels = {channel for channel, _model in bound_routes}
            keys = {key for key in by_model.get(model_id, ()) if key[0] not in bound_channels}
            keys.update(bound_routes)
            raw = self.log_db._new_token_stats_agg()
            for key in keys:
                if channel_key is not None and key[0] != channel_key:
                    continue
                row = routes.get(key)
                if row is not None:
                    self.log_db._merge_token_stats_agg(raw, row)
            metrics = self.log_db._pack_stats(raw)
            # Tokens from failed attempts can exist without a final-route request.
            if metrics["total"] or any(metrics[k] for k in (
                "input", "output", "cache_creation", "cache_read",
            )):
                result[model_id] = {key: metrics[key] for key in (
                    "total", "success_count", "error_count", "input", "output",
                    "cache_creation", "cache_read", "avg_tps", "max_tps", "min_tps",
                )}
        return result

    def channel_model_stats(self, context: ManagementContext, channel_key: str, since_ts: float) -> list[dict]:
        require(context)
        return copy.deepcopy(self.log_db.channel_model_stats(channel_key, since_ts=float(since_ts)))

    def apikey_model_stats(self, context: ManagementContext, name: str, since_ts: float) -> list[dict]:
        require(context)
        return copy.deepcopy(self.log_db.apikey_model_stats(name, since_ts=float(since_ts)))


DEFAULT_STATS_CONTROL = StatsControl()
