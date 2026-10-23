"""Period-over-period request statistics (``GET /stats/compare``).

Both windows go through the same bucketed aggregation as ``/stats/timeseries``,
so the change between them is measured with one set of rules.  The previous
window always covers the same elapsed time as the current one: today is
compared with yesterday up to the same clock time, this month with last month
up to the same point, a rolling or custom range with the range right before it.

Hours that are over are aggregated once, per model × channel × API key, and
kept (:class:`HourCache`); a window is then a sum over them plus live queries
for its partial edge hours and the hours that may still change.  Any filter is
a selection of those keys, so a month for one channel costs no more than
the month itself.
"""

from __future__ import annotations

import math
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from src.management_control.errors import ErrorField, ManagementError, ManagementErrorCode

from ._log_errors import map_historical_log_errors
from .common import revision_for
from .timeseries import _BJT, _merge, _point

# Sparkline resolution: the finest step that keeps the series at most this long
# (today → hours, 3 days → 3 h, 7 days → 6 h, a month → days). Windows of six hours
# or more use whole hours so their buckets are sums of cached hours.
MAX_POINTS = 32
_STEPS = (60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800)
_ROLLING = {"3d": 3 * 86400, "7d": 7 * 86400}
_FILTER_DIMS = (("model", "model"), ("channel", "channel"), ("apiKey", "apikey"))
_KEY_DIMS = ("model", "channel", "apikey")
HOUR = 3600
# A request is logged when it starts and settles (status, attempt costs) when it ends; an hour
# that ended this long ago no longer changes.
SETTLE_SECONDS = 3600
# Oldest hour kept: the start of last month is at most ~62 days back.
KEEP_SECONDS = 70 * 86400


def _invalid(field: str, code: str, message: str) -> ManagementError:
    return ManagementError(ManagementErrorCode.VALIDATION_FAILED, fields=(ErrorField(field, code, message),))


def compare_windows(
    now: float, *, period: str | None, start: float | None, end: float | None,
) -> tuple[float, float, float, float]:
    """``(start, end, previous_start, previous_end)`` as epoch seconds."""
    if period is not None:
        if start is not None or end is not None:
            raise _invalid("period", "conflict", "use either period or from/to")
        local = datetime.fromtimestamp(now, tz=_BJT)
        if period == "today":
            current = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            return current, now, current - 86400, now - 86400
        if period == "month":
            first = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            previous = (first - timedelta(days=1)).replace(day=1).timestamp()
            current = first.timestamp()
            return current, now, previous, min(previous + (now - current), current)
        if period in _ROLLING:
            span = _ROLLING[period]
            return now - span, now, now - 2 * span, now - span
        raise _invalid("period", "unsupported", "period must be today, 3d, 7d or month")
    if start is None or end is None:
        raise _invalid("from", "required", "period or both from and to are required")
    end = min(float(end), now)
    if end <= start:
        raise _invalid("to", "out_of_range", "to must be later than from and not in the future")
    span = end - start
    return float(start), end, float(start) - span, float(start)


def _step(span: float) -> int:
    for step in _STEPS:
        if step < HOUR and span >= 6 * HOUR:
            continue
        if math.ceil(span / step) <= MAX_POINTS:
            return step
    return _STEPS[-1]


def _floor_hour(ts: float) -> int:
    return int(ts // HOUR * HOUR)


def _ceil_hour(ts: float) -> int:
    return int(-(-ts // HOUR) * HOUR)


class HourCache:
    """Closed hours of bucketed statistics, keyed ``model \x1f channel \x1f apikey``.

    Valid for one log source and one pricing configuration: a pricing change (costs are
    estimated from it) or a different source clears it. Hours older than the oldest kept
    request (retention) are dropped when a window reaches them.
    """

    def __init__(self) -> None:
        self._hours: dict[int, dict[str, dict[str, int]]] = {}
        self._signature: tuple | None = None
        self._lock = threading.Lock()

    def closed(self, source, hours: list[int], signature: tuple, earliest: float | None) -> dict[int, dict[str, dict[str, int]]]:
        with self._lock:
            if signature != self._signature:
                self._hours.clear()
                self._signature = signature
            # Nothing is logged before the oldest kept request: those hours are empty, not unknown.
            floor = _floor_hour(earliest) if earliest is not None else None
            for hour in [h for h in self._hours if floor is None or h < floor]:
                del self._hours[hour]
            missing = sorted(h for h in hours if h not in self._hours and floor is not None and h >= floor)
            run: list[int] = []
            for hour in missing + [None]:
                if run and (hour is None or hour != run[-1] + HOUR):
                    raw = source.bucketed_stats(start=run[0], bucket_seconds=HOUR, count=len(run), group=_KEY_DIMS)
                    for h in run:
                        self._hours[h] = {}
                    for (index, key), point in raw.items():
                        self._hours[run[0] + index * HOUR][key] = point
                    run = []
                if hour is not None:
                    run.append(hour)
            oldest = max(hours or [0]) - KEEP_SECONDS
            for hour in [h for h in self._hours if h < oldest]:
                del self._hours[hour]
            return {h: self._hours.get(h, {}) for h in hours}


def _metrics(raw: dict[str, int] | None) -> dict[str, Any]:
    out = _point(0, raw)
    out.pop("t")
    out["unpricedSuccess"] = int((raw or {}).get("unpriced_success") or 0)
    out["cacheHitRequests"] = int((raw or {}).get("hit_requests") or 0)
    out["affinityHits"] = int((raw or {}).get("affinity_hits") or 0)
    # Same weighting as the summary: generated tokens over generation time.
    denom = int((raw or {}).get("tps_denom_ms") or 0)
    out["averageTokensPerSecond"] = int((raw or {}).get("tps_num_tokens") or 0) * 1000.0 / denom if denom > 0 else None
    return out


def _pricing_signature() -> tuple:
    from src import model_pricing
    return (repr(model_pricing.settings()),)


def build_compare(
    source,
    *,
    now: float,
    period: str | None = None,
    start: float | None = None,
    end: float | None = None,
    filters: dict[str, str | None] | None = None,
    hours: HourCache | None = None,
) -> dict[str, Any]:
    start, end, previous_start, previous_end = compare_windows(now, period=period, start=start, end=end)
    wanted = [(_KEY_DIMS.index(dim), value) for name, dim in _FILTER_DIMS if (value := (filters or {}).get(name))]

    def matches(key: str) -> bool:
        parts = key.split("\x1f")
        return all(parts[i] == value for i, value in wanted)

    def add(target: dict[str, int], points: dict[str, dict[str, int]]) -> None:
        for key, point in points.items():
            if matches(key):
                _merge(target, point)

    with map_historical_log_errors():
        # A previous window that predates the oldest kept log is not comparable: its zero would be
        # retention, not traffic. Injected sources without the probe are taken as complete.
        probe = getattr(source, "earliest_request_at", None)
        earliest = probe() if probe is not None else previous_start
        signature = _pricing_signature()
        cache = hours if hours is not None else HourCache()
        settled = _floor_hour(now - SETTLE_SECONDS)

        def pieces(a: float, b: float) -> list[tuple[float, dict[str, dict[str, int]]]]:
            """``[(piece start, points by key)]`` covering ``[a, b)``: cached closed hours, live edges."""
            out: list[tuple[float, dict[str, dict[str, int]]]] = []
            head_end = min(_ceil_hour(a), b)
            if head_end > a:
                raw = source.bucketed_stats(start=a, bucket_seconds=max(1, math.ceil(head_end - a)), count=1,
                                            group=_KEY_DIMS, end=head_end)
                out.append((a, {key: p for (_i, key), p in raw.items()}))
            first, last = _ceil_hour(a), _floor_hour(b)
            closed = [h for h in range(first, min(last, settled), HOUR)] if last > first else []
            for hour, points in sorted(cache.closed(source, closed, signature, earliest).items()):
                out.append((hour, points))
            live_from = max(first, min(last, settled)) if last > first else max(first, _floor_hour(b))
            if b > live_from and live_from >= head_end:
                count = math.ceil((b - live_from) / HOUR)
                raw = source.bucketed_stats(start=live_from, bucket_seconds=HOUR, count=count, group=_KEY_DIMS, end=b)
                by_hour: dict[int, dict[str, dict[str, int]]] = {}
                for (index, key), point in raw.items():
                    by_hour.setdefault(index, {})[key] = point
                out.extend((live_from + index * HOUR, points) for index, points in sorted(by_hour.items()))
            return out

        step = _step(end - start)
        if step < HOUR:
            # Short windows: a direct query at the sparkline's own resolution.
            origin = start
            count = max(1, math.ceil((end - start) / step))
            raw = source.bucketed_stats(start=start, bucket_seconds=step, count=count, group=_KEY_DIMS, end=end)
            buckets: dict[int, dict[str, int]] = {}
            for (index, key), point in raw.items():
                if matches(key):
                    _merge(buckets.setdefault(index, {}), point)
        else:
            # Buckets on whole hours (on Beijing midnight for days) so each piece falls in one bucket.
            origin = start if step >= 86400 and start == _floor_hour(start) else _floor_hour(start)
            count = max(1, math.ceil((end - origin) / step))
            buckets = {}
            for at, points in pieces(start, end):
                add(buckets.setdefault(int((at - origin) // step), {}), points)
        previous: dict[str, int] = {}
        for _at, points in pieces(previous_start, previous_end):
            add(previous, points)
    complete = earliest is not None and earliest <= previous_start

    current: dict[str, int] = {}
    for point in buckets.values():
        _merge(current, point)
    utc = timezone.utc
    data = {
        "from": datetime.fromtimestamp(start, tz=utc),
        "to": datetime.fromtimestamp(end, tz=utc),
        "previousFrom": datetime.fromtimestamp(previous_start, tz=utc),
        "previousTo": datetime.fromtimestamp(previous_end, tz=utc),
        "previousComplete": complete,
        "bucketSeconds": step,
        "current": _metrics(current),
        "previous": _metrics(previous),
        "points": [
            {"t": datetime.fromtimestamp(origin + index * step, tz=utc), **_metrics(buckets.get(index))}
            for index in range(count)
        ],
    }
    data["revision"] = revision_for(data)
    return data
