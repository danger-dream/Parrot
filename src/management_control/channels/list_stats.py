"""Month statistics for the channel list (TG 「💎 Parrot 月度」, round 3).

Uses the shared month snapshot (``by_channel``, same buckets as
``tokens_for_channel``) through the non-blocking statistics cache; while the
first computation runs the list reports ``monthStatsLoading`` exactly like TG's
「统计初始化中」.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from src import log_db
from src.management_auth import AuthMethod


def month_start() -> float:
    now = datetime.now(timezone(timedelta(hours=8)))
    return datetime(now.year, now.month, 1, tzinfo=now.tzinfo).timestamp()


def month_stats_by_channel(context) -> tuple[dict[str, dict[str, Any]], bool]:
    from src.management_control.stats_cache import STATS_CACHE, snapshot_key

    if context is not None and context.actor.auth_method is AuthMethod.TELEGRAM_ADMIN:
        return {}, False
    since = month_start()
    value, loading = STATS_CACHE.peek_or_refresh(
        snapshot_key("month", since), lambda: log_db.stats_period_snapshot(since),
    )
    return dict((value or {}).get("by_channel") or {}), loading
