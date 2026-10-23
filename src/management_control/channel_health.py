"""Channel health derived from the scorer's per-model recent windows.

A window holds a model's last N requests however old they are, so a model that
failed days ago and has not been routed to since would keep its channel flagged
forever. Only windows that saw a request within ``ACTIVE_WINDOW_MS`` count.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

ACTIVE_WINDOW_MS = 3 * 3600 * 1000
HEALTHY_RATE = 80.0
DEGRADED_RATE = 50.0


def active_model_rates(rows: Iterable[dict[str, Any]], *, now_ms: int | None = None) -> list[dict[str, Any]]:
    """Recent success rate per model still in use, lowest first."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    out: list[dict[str, Any]] = []
    for row in rows:
        requests = int(row.get("recent_requests") or 0)
        updated = int(row.get("last_updated") or 0)
        if requests <= 0 or now - updated > ACTIVE_WINDOW_MS:
            continue
        successes = int(row.get("recent_success_count") or 0)
        out.append({
            "model": str(row.get("model") or ""),
            "successRate": successes / requests * 100,
            "recentRequests": requests,
            "recentSuccessCount": successes,
        })
    out.sort(key=lambda item: (item["successRate"], item["model"]))
    return out


def rate_health(rate: float | None) -> str:
    if rate is None:
        return "unknown"
    if rate >= HEALTHY_RATE:
        return "healthy"
    if rate >= DEGRADED_RATE:
        return "degraded"
    return "unhealthy"
