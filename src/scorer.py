"""智能渠道评分（基于 openai-proxy/scorer.js 的 Python 等价移植）。

策略：
  1. 滑动窗口 EMA（默认 50）：老数据自然淡出
  2. 无 MIN_SAMPLES 混合：样本少的渠道直接用实测值
  3. 探索率 0.2：五分之一请求故意选"最值得探索"的渠道
  4. 陈旧衰减：15 min 开始向默认分漂移，30 min 完全回归

评分公式：
  recent_success_rate = min(recent_success, recent_total) / recent_total
  latency = avg_connect_ms + avg_first_byte_ms
  penalty = 1 + (1 - recent_success_rate) * errorPenaltyFactor (默认 8)
  score   = latency * penalty
  stale decay：按时间线性插值向 DEFAULT_SCORE 漂移

分数越低越好。
"""

from __future__ import annotations

import random
import threading
import time
from typing import Optional

from . import config, state_db


_lock = threading.Lock()
_stats: dict[tuple[str, str], dict] = {}
_initialized = False


def _now_ms() -> int:
    return int(time.time() * 1000)


def _params() -> dict:
    cfg = config.get()
    s = cfg.get("scoring", {}) or {}
    return {
        "emaAlpha": float(s.get("emaAlpha", 0.25)),
        "recentWindow": int(s.get("recentWindow", 50)),
        "defaultScore": float(s.get("defaultScore", 3000)),
        "errorPenaltyFactor": float(s.get("errorPenaltyFactor", 8)),
        "staleMinutes": float(s.get("staleMinutes", 15)),
        "staleFullDecayMinutes": float(s.get("staleFullDecayMinutes", 30)),
        "explorationRate": float(s.get("explorationRate", 0.2)),
    }


def init() -> None:
    global _initialized
    if _initialized:
        return
    rows = state_db.perf_load_all()
    params = _params()
    window = params["recentWindow"]
    with _lock:
        _stats.clear()
        for row in rows:
            # 历史累积数据无法还原滑动窗口，按全量成功率等比推算，并 cap 到窗口上限
            total = int(row.get("total_requests") or 0)
            succ = int(row.get("success_count") or 0)
            capped_recent = min(total, window)
            rate = (succ / total) if total > 0 else 0
            capped_succ = int(round(capped_recent * rate))
            _stats[(row["channel_key"], row["model"])] = {
                "total_requests": total,
                "success_count": succ,
                "recent_requests": capped_recent,
                "recent_success_count": capped_succ,
                "avg_connect_ms": float(row.get("avg_connect_ms") or 0),
                "avg_first_byte_ms": float(row.get("avg_first_byte_ms") or 0),
                "avg_total_ms": float(row.get("avg_total_ms") or 0),
                "last_updated": int(row.get("last_updated") or 0),
            }
    _initialized = True
    print(f"[scorer] loaded {len(rows)} entries from state.db")


def _get(channel_key: str, model: str) -> Optional[dict]:
    with _lock:
        v = _stats.get((channel_key, model))
        return dict(v) if v else None


def get_stats(channel_key: str, model: str) -> Optional[dict]:
    return _get(channel_key, model)


def calculate_score(stats: Optional[dict], params: Optional[dict] = None) -> float:
    if params is None:
        params = _params()
    default = params["defaultScore"]
    if not stats or stats.get("total_requests", 0) == 0:
        return default

    window = params["recentWindow"]
    recent_total = min(int(stats.get("recent_requests", 0)), window)
    if recent_total > 0:
        rate = min(int(stats.get("recent_success_count", 0)), recent_total) / recent_total
    else:
        tot = stats.get("total_requests", 0) or 1
        rate = (stats.get("success_count", 0) or 0) / tot

    latency = float(stats.get("avg_connect_ms", 0) or 0) + float(stats.get("avg_first_byte_ms", 0) or 0)
    penalty = 1 + (1 - rate) * params["errorPenaltyFactor"]
    score = latency * penalty

    # 陈旧衰减
    stale_min = (_now_ms() - int(stats.get("last_updated", 0))) / 60000
    lo = params["staleMinutes"]
    hi = params["staleFullDecayMinutes"]
    if hi <= lo:
        hi = lo + 1
    if stale_min > lo:
        progress = min((stale_min - lo) / (hi - lo), 1.0)
        score = score * (1 - progress) + default * progress

    return score


def get_score(channel_key: str, model: str) -> float:
    return calculate_score(_get(channel_key, model))


# ─── 记录成功 / 失败 ─────────────────────────────────────────────

def record_success(channel_key: str, model: str,
                   connect_ms: Optional[float], first_byte_ms: Optional[float],
                   total_ms: Optional[float]) -> None:
    params = _params()
    window = params["recentWindow"]
    alpha = params["emaAlpha"]

    with _lock:
        stats = _stats.get((channel_key, model))
        if stats is None:
            stats = {
                "total_requests": 0, "success_count": 0,
                "recent_requests": 0, "recent_success_count": 0,
                "avg_connect_ms": float(connect_ms or 0),
                "avg_first_byte_ms": float(first_byte_ms or 0),
                "avg_total_ms": float(total_ms or 0),
                "last_updated": _now_ms(),
            }
            _stats[(channel_key, model)] = stats

        stats["total_requests"] += 1
        stats["success_count"] += 1
        stats["last_updated"] = _now_ms()

        # 滑动窗口
        if stats["recent_requests"] < window:
            stats["recent_requests"] += 1
            stats["recent_success_count"] += 1
        else:
            old_rate = stats["recent_success_count"] / window
            stats["recent_success_count"] = min(
                window,
                int(round(stats["recent_success_count"] - old_rate + 1)),
            )

        # EMA 延迟
        if stats["total_requests"] == 1:
            stats["avg_connect_ms"] = float(connect_ms or 0)
            stats["avg_first_byte_ms"] = float(first_byte_ms or 0)
            stats["avg_total_ms"] = float(total_ms or 0)
        else:
            if connect_ms is not None:
                stats["avg_connect_ms"] = alpha * float(connect_ms) + (1 - alpha) * stats["avg_connect_ms"]
            if first_byte_ms is not None:
                stats["avg_first_byte_ms"] = alpha * float(first_byte_ms) + (1 - alpha) * stats["avg_first_byte_ms"]
            if total_ms is not None:
                stats["avg_total_ms"] = alpha * float(total_ms) + (1 - alpha) * stats["avg_total_ms"]

        snapshot = dict(stats)

    state_db.perf_save(channel_key, model, snapshot)


def record_failure(channel_key: str, model: str, connect_ms: Optional[float]) -> None:
    params = _params()
    window = params["recentWindow"]
    alpha = params["emaAlpha"]

    with _lock:
        stats = _stats.get((channel_key, model))
        if stats is None:
            stats = {
                "total_requests": 0, "success_count": 0,
                "recent_requests": 0, "recent_success_count": 0,
                "avg_connect_ms": float(connect_ms) if connect_ms is not None else params["defaultScore"],
                "avg_first_byte_ms": 0.0,
                "avg_total_ms": 0.0,
                "last_updated": _now_ms(),
            }
            _stats[(channel_key, model)] = stats

        stats["total_requests"] += 1
        stats["last_updated"] = _now_ms()

        if stats["recent_requests"] < window:
            stats["recent_requests"] += 1
        else:
            old_rate = stats["recent_success_count"] / window
            stats["recent_success_count"] = max(
                0,
                int(round(stats["recent_success_count"] - old_rate)),
            )

        if connect_ms is not None and stats["total_requests"] > 1:
            stats["avg_connect_ms"] = alpha * float(connect_ms) + (1 - alpha) * stats["avg_connect_ms"]

        snapshot = dict(stats)

    state_db.perf_save(channel_key, model, snapshot)


# ─── 排序 + 探索 ────────────────────────────────────────────────

def sort_by_score(candidates: list, exploration_rate: Optional[float] = None,
                  rng: Optional[random.Random] = None) -> list:
    """按 score 升序排，概率性把"最值得探索"的候选抬到首位。

    candidates: list[(Channel, resolved_model)]
    """
    if len(candidates) <= 1:
        return list(candidates)
    params = _params()
    rate = params["explorationRate"] if exploration_rate is None else float(exploration_rate)
    _rng = rng if rng is not None else random

    # 先按 score 升序（低分好）排
    scored = sorted(candidates, key=lambda ca: calculate_score(_get(ca[0].key, ca[1]), params))

    if rate > 0 and _rng.random() < rate:
        idx = _pick_explore_target(scored)
        if idx is not None and idx != 0:
            scored.insert(0, scored.pop(idx))

    return scored


def _pick_explore_target(ordered: list) -> Optional[int]:
    """选值得探索的目标。优先级：
      - 未测过（total_requests=0）→ priority=1_000_000
      - 否则 priority = (stale_min + 1) / (recent_count + 1)
    """
    best_idx: Optional[int] = None
    best_priority = -1.0
    for i, (ch, model) in enumerate(ordered):
        stats = _get(ch.key, model)
        if not stats or stats.get("total_requests", 0) == 0:
            priority = 1_000_000.0
        else:
            recent_count = min(int(stats.get("recent_requests", 0)),
                               int(_params()["recentWindow"]))
            stale_min = (_now_ms() - int(stats.get("last_updated", 0))) / 60000
            priority = (stale_min + 1) / (recent_count + 1)
        if priority > best_priority:
            best_priority = priority
            best_idx = i
    return best_idx


# ─── 维护 ─────────────────────────────────────────────────────────

def clear_stats(channel_key: Optional[str] = None, model: Optional[str] = None) -> None:
    with _lock:
        if channel_key and model:
            _stats.pop((channel_key, model), None)
        elif channel_key:
            for k in [k for k in _stats if k[0] == channel_key]:
                _stats.pop(k, None)
        else:
            _stats.clear()
    state_db.perf_delete(channel_key, model)


def rename_channel(old_key: str, new_key: str) -> None:
    if old_key == new_key:
        return
    with _lock:
        items = [(k, v) for k, v in _stats.items() if k[0] == old_key]
        for (_, m), v in items:
            _stats.pop((old_key, m), None)
            _stats[(new_key, m)] = v
    state_db.perf_rename_channel(old_key, new_key)


def snapshot() -> list[dict]:
    """供 TG Bot 渲染用。"""
    params = _params()
    out: list[dict] = []
    with _lock:
        for (ck, m), s in _stats.items():
            score = calculate_score(s, params)
            out.append({
                "channel_key": ck,
                "model": m,
                "total_requests": s["total_requests"],
                "success_count": s["success_count"],
                "recent_requests": s["recent_requests"],
                "recent_success_count": s["recent_success_count"],
                "avg_connect_ms": round(s["avg_connect_ms"]),
                "avg_first_byte_ms": round(s["avg_first_byte_ms"]),
                "avg_total_ms": round(s["avg_total_ms"]),
                "last_updated": s["last_updated"],
                "score": round(score),
            })
    return out
