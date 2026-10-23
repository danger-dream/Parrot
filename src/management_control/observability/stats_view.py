"""Derived statistics TG renders from the same aggregates (round 3).

* ``with_rates``: 读入 (prompt = input + cache write + cache read), 缓存占比,
  成功率, 命中请求率, 重试/亲和比例 — exactly TG's ``fmt_rate`` inputs.
* ``summary_tops``: the TG 汇总 Top blocks (``stats_menu._render_overall``).
"""

from __future__ import annotations

from typing import Any, Callable


def _ratio(num: Any, denom: Any) -> float | None:
    try:
        num, denom = float(num or 0), float(denom or 0)
    except (TypeError, ValueError):
        return None
    return num / denom if denom > 0 else None


def with_rates(metrics: dict[str, Any]) -> dict[str, Any]:
    prompt = metrics["inputTokens"] + metrics["cacheCreationTokens"] + metrics["cacheReadTokens"]
    return {
        **metrics,
        "promptTokens": prompt,
        "cacheReadRatio": _ratio(metrics["cacheReadTokens"], prompt),
        "successRate": _ratio(metrics["successCount"], metrics["total"]),
        "cacheHitRate": _ratio(metrics["cacheHitRequests"], metrics["successCount"]),
        "cacheWriteRate": _ratio(metrics["cacheWriteRequests"], metrics["successCount"]),
        "retriedRequestRate": _ratio(metrics["retriedRequests"], metrics["total"]),
        "affinityHitRate": _ratio(metrics["affinityHits"], metrics["total"]),
    }


def _rows(result: Any, key: str) -> list[dict]:
    rows = (result or {}).get(key) if isinstance(result, dict) else None
    if isinstance(rows, dict):
        rows = [{"key": name, "metrics": value} for name, value in rows.items()]
    return [row for row in rows or [] if isinstance(row, dict) and (row.get("key") or "?") != "?"]


def summary_tops(snapshot: dict, families: Any, normalize: Callable[[Any], dict]) -> dict[str, Any]:
    model_channels = snapshot.get("model_channels") if isinstance(snapshot.get("model_channels"), dict) else {}
    tops: dict[str, Any] = {}
    split: dict[str, dict[str, int]] = {}
    for family, result in (families.items() if isinstance(families, dict) else ()):
        tops[str(family)] = {
            "channels": [{"key": str(row["key"]), "metrics": normalize(row.get("metrics"))}
                         for row in _rows(result, "by_channel")[:3]],
            "models": [{
                "key": str(row["key"]), "metrics": normalize(row.get("metrics")),
                "channels": [{"key": str(item.get("key") or "?"), "type": item.get("type"),
                              "count": int(item.get("count") or 0)}
                             for item in (model_channels.get(row["key"]) or []) if isinstance(item, dict)],
            } for row in _rows(result, "by_model")[:3]],
        }
        for row in _rows(result, "by_apikey"):
            split.setdefault(str(row["key"]), {})[str(family)] = int((row.get("metrics") or {}).get("total") or 0)
    summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else snapshot
    keys = [{"key": str(row["key"]), "metrics": normalize(row.get("metrics")),
             "familyTotals": split.get(str(row["key"]), {})}
            for row in _rows(summary, "by_apikey")[:3]]
    return {"familyTops": tops, "topApiKeys": keys}
