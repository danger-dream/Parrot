"""Time-bucketed request statistics over the monthly request-log databases.

The aggregation deliberately reuses ``log_db``'s summary rules so the series
add up to the same numbers as ``stats_summary`` for the same window:

* request counts/latency come from ``request_log`` (``errorCount`` counts only
  ``status='error'`` like the summary; cancelled/pending are in ``total`` only);
* Token totals replace final-request usage with every observed upstream attempt
  when the immutable attempt ledger exists (``_replace_summary_tokens_with_attempts``);
* cost follows ``_accumulate_usage_costs``: settled attempt ledger first
  (actual or estimated per attempt), otherwise the legacy per-request estimate,
  and xAI OAuth responses prefer their reported actual cost.

Buckets are ``floor((created_at - start) / bucket_seconds)``; callers align
``start`` (day buckets to Beijing midnight, the same fixed UTC+8 zone the
existing "today/month" statistics use). Only SQL GROUP BY results are
materialized, never individual request rows (except xAI rows, which need their
response tail exactly as the summary does).
"""

from __future__ import annotations

import os
import re
import sqlite3
from typing import Any

from . import log_db, model_pricing


_GROUP_EXPRS = {
    None: ("'total'", "'total'"),
    "model": (
        "COALESCE(request_log.requested_model, '?')",
        "COALESCE(request_log.requested_model, '?')",
    ),
    "channel": (
        "COALESCE(request_log.final_channel_key, '?')",
        "COALESCE(a.channel_key, '?')",
    ),
    "apikey": (
        "COALESCE(request_log.api_key_name, '?')",
        "COALESCE(request_log.api_key_name, '?')",
    ),
}
_COST_ATTEMPT_CHANNEL = "COALESCE(a.channel_key, request_log.final_channel_key, '?')"
# Composite keys (a tuple ``group``) join their parts with this; it never occurs in
# model names, channel keys or API-key names.
KEY_SEPARATOR = "\x1f"


# Key of a dispatched retry that never settled (``retry_chain r``), counted as unpriced.
_RETRY_EXPRS = {
    None: "'total'",
    "model": _GROUP_EXPRS["model"][0],
    "channel": "COALESCE(r.channel_key, '?')",
    "apikey": _GROUP_EXPRS["apikey"][0],
}


def _group_exprs(group: str | tuple[str, ...] | None) -> tuple[str, str, str, str]:
    """``(request, attempt, cost-attempt, unsettled-retry)`` key SQL for one dimension or a tuple."""
    parts = group if isinstance(group, tuple) else (group,)
    if not parts or any(part not in _GROUP_EXPRS for part in parts) or (len(parts) > 1 and None in parts):
        raise ValueError("unsupported time-series grouping")
    exprs = [
        (*_GROUP_EXPRS[part], _COST_ATTEMPT_CHANNEL if part == "channel" else _GROUP_EXPRS[part][1], _RETRY_EXPRS[part])
        for part in parts
    ]
    if len(exprs) == 1:
        return exprs[0]
    return tuple(" || char(31) || ".join(e[i] for e in exprs) for i in range(4))  # type: ignore[return-value]


def new_point() -> dict[str, int]:
    return {
        "total": 0, "success_count": 0, "error_count": 0, "cancelled_count": 0, "hit_requests": 0, "affinity_hits": 0,
        "tps_num_tokens": 0, "tps_denom_ms": 0,
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "cost_ticks": 0, "unpriced_success": 0,
        "sum_first_token_ms": 0, "cnt_first_token": 0,
        "sum_total_ms": 0, "cnt_total": 0,
    }


def _bucket_expr(column: str = "request_log.created_at") -> str:
    return f"CAST(({column} - ?) / ? AS INTEGER)"


def _target(
    out: dict[tuple[int, str], dict[str, int]], bucket: Any, key: Any, count: int,
) -> dict[str, int] | None:
    try:
        index = int(bucket)
    except (TypeError, ValueError):
        return None
    if index < 0 or index >= count:
        return None
    return out.setdefault((index, str(key or "?")), new_point())


def _accumulate_counts(conn, out, *, start, end, bucket_seconds, count, key_expr) -> None:
    rows = conn.execute(
        f"""SELECT {_bucket_expr()} AS b, {key_expr} AS k,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
                   SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS error_count,
                   SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS cancelled_count,
                   SUM(CASE WHEN status='success' AND cache_read_tokens > 0 THEN 1 ELSE 0 END) AS hit_requests,
                   SUM(CASE WHEN affinity_hit=1 THEN 1 ELSE 0 END) AS affinity_hits,
                   SUM(CASE WHEN {log_db._TPS_COND} THEN output_tokens ELSE 0 END) AS tps_num_tokens,
                   SUM(CASE WHEN {log_db._TPS_COND} THEN {log_db._TPS_DENOM_MS_EXPR} ELSE 0 END) AS tps_denom_ms,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cache_read_tokens) AS cache_read_tokens,
                   SUM(cache_creation_tokens) AS cache_creation_tokens,
                   SUM(CASE WHEN status='success' AND is_stream=1 AND first_token_time_ms IS NOT NULL
                            THEN first_token_time_ms ELSE 0 END) AS sum_first_token_ms,
                   SUM(CASE WHEN status='success' AND is_stream=1 AND first_token_time_ms IS NOT NULL
                            THEN 1 ELSE 0 END) AS cnt_first_token,
                   SUM(CASE WHEN status='success' AND total_time_ms IS NOT NULL
                            THEN total_time_ms ELSE 0 END) AS sum_total_ms,
                   SUM(CASE WHEN status='success' AND total_time_ms IS NOT NULL
                            THEN 1 ELSE 0 END) AS cnt_total
              FROM request_log
             WHERE request_log.created_at >= ? AND request_log.created_at < ?
             GROUP BY b, k""",
        (start, bucket_seconds, start, end),
    ).fetchall()
    for row in rows:
        target = _target(out, row["b"], row["k"], count)
        if target is None:
            continue
        for name in (
            "total", "success_count", "error_count", "cancelled_count", "hit_requests", "affinity_hits",
            "tps_num_tokens", "tps_denom_ms", "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_creation_tokens", "sum_first_token_ms",
            "cnt_first_token", "sum_total_ms", "cnt_total",
        ):
            target[name] += int(row[name] or 0)


def _replace_tokens_with_attempts(
    conn, out, *, start, end, bucket_seconds, count, key_expr, attempt_key_expr,
) -> None:
    if not log_db._attempt_rows_exist(conn):
        return
    window = "request_log.created_at >= ? AND request_log.created_at < ?"
    roots = log_db._token_aggregate_rows(
        conn,
        f"""SELECT {_bucket_expr()} AS b, {key_expr} AS k,
                   {log_db._TOKEN_SUM_TOKENS['inp']} AS inp,
                   {log_db._TOKEN_SUM_TOKENS['outp']} AS outp,
                   {log_db._TOKEN_SUM_TOKENS['cc']} AS cc,
                   {log_db._TOKEN_SUM_TOKENS['cr']} AS cr
              FROM request_log
             WHERE {window} AND {log_db._final_observed_attempt_sql(conn)}
             GROUP BY b, k""",
        (start, bucket_seconds, start, end),
        expressions={
            "inp": "request_log.input_tokens",
            "outp": "request_log.output_tokens",
            "cc": "request_log.cache_creation_tokens",
            "cr": "request_log.cache_read_tokens",
        },
    )
    effective = log_db._effective_attempt_exprs(conn, alias="a")
    attempts = log_db._token_aggregate_rows(
        conn,
        f"""SELECT {_bucket_expr()} AS b, {attempt_key_expr} AS k,
                   {log_db._TOKEN_SUM_TOKENS['inp']} AS inp,
                   {log_db._TOKEN_SUM_TOKENS['outp']} AS outp,
                   {log_db._TOKEN_SUM_TOKENS['cc']} AS cc,
                   {log_db._TOKEN_SUM_TOKENS['cr']} AS cr
              FROM request_log CROSS JOIN upstream_attempt_usage a
                ON a.root_request_id=request_log.request_id
             WHERE {window}
               AND ({effective['usage_observed']})=1
               AND ({effective['included']})=1
               AND COALESCE(a.call_request_id, '')<>''
             GROUP BY b, k""",
        (start, bucket_seconds, start, end),
        expressions={
            "inp": effective["input"],
            "outp": effective["output"],
            "cc": effective["cache_creation"],
            "cr": effective["cache_read"],
        },
    )
    for rows, sign in ((roots, -1), (attempts, 1)):
        for row in rows:
            target = _target(out, row["b"], row["k"], count)
            if target is None:
                continue
            target["input_tokens"] += sign * int(row["inp"] or 0)
            target["output_tokens"] += sign * int(row["outp"] or 0)
            target["cache_creation_tokens"] += sign * int(row["cc"] or 0)
            target["cache_read_tokens"] += sign * int(row["cr"] or 0)


def _accumulate_costs(
    conn, out, *, start, end, bucket_seconds, count, key_expr, cost_attempt_key_expr,
    retry_key_expr="'total'",
) -> None:
    pricing_settings = model_pricing.settings()
    if not pricing_settings.enabled:
        return
    window = "request_log.created_at >= ? AND request_log.created_at < ?"
    window_args = (start, end)
    bucket_args = (start, bucket_seconds)

    def add(target: dict[str, int] | None, ticks: Any) -> None:
        if target is not None:
            target["cost_ticks"] += max(0, int(ticks or 0))

    if log_db._attempt_rows_exist(conn):
        rows = log_db._cost_aggregate_rows(
            conn,
            f"""SELECT {_bucket_expr()} AS b, {cost_attempt_key_expr} AS k,
                       a.cost_source, COUNT(*) AS row_count,
                       {log_db._COST_TICKS_SUM_TOKEN} AS cost_ticks
                  FROM request_log CROSS JOIN upstream_attempt_usage a
                    ON a.root_request_id=request_log.request_id
                 WHERE {window}
                 GROUP BY b, k, a.cost_source""",
            bucket_args + window_args,
            cost_ticks_expr="a.cost_ticks",
        )
        for row in rows:
            target = _target(out, row["b"], row["k"], count)
            if str(row["cost_source"] or "") in {"actual", "estimated"}:
                add(target, row["cost_ticks"])
            elif target is not None:
                target["unpriced_success"] += int(row["row_count"] or 0)

    # Dispatched retries that crashed before settlement are unpriced, as in the summary.
    missing = log_db._missing_dispatch_rows(
        conn,
        select_sql=f"{_bucket_expr()} AS b, {retry_key_expr} AS k, COUNT(*) AS row_count",
        where_sql=window,
        args=bucket_args + window_args,
        group_by_sql="b, k",
        # A short window is a narrow created_at range: probing its requests beats scanning retries.
        request_first=end - start <= 2 * 86400 or log_db._prefer_request_first_join(start),
    )
    for row in missing:
        target = _target(out, row["b"], row["k"], count)
        if target is not None:
            target["unpriced_success"] += int(row["row_count"] or 0)

    pricing_exprs = log_db._request_pricing_exprs(conn)
    attempt_exclusion = log_db._attempt_exclusion_sql(conn)
    model_expr = "COALESCE(final_model, requested_model, '?')"
    channel_expr = "COALESCE(final_channel_key, '?')"
    prompt_expr = (
        "COALESCE(input_tokens, 0) + COALESCE(cache_creation_tokens, 0) "
        "+ COALESCE(cache_read_tokens, 0)"
    )
    # Without ANALYZE statistics SQLite picks the status index and walks every successful request
    # of the month even for a one-hour window; unary + keeps short windows on the created_at range.
    short = end - start <= 2 * 86400
    status = "+status" if short else "status"
    xai_status = "+request_log.status" if short else "request_log.status"
    standard_where = (
        f"{window} AND {status}='success' {attempt_exclusion}"
        "AND COALESCE(final_channel_key, '') NOT LIKE 'oauth:xai:%'"
    )
    pricing_pairs = log_db._pricing_model_channel_rows(
        conn, model_expr=model_expr, channel_expr=channel_expr,
        where_sql=standard_where, where_args=window_args,
    )
    long_case, long_args = log_db._long_context_case_sql(
        conn, model_expr=model_expr, channel_expr=channel_expr,
        prompt_expr=prompt_expr, where_sql=standard_where, where_args=window_args,
        pricing_settings=pricing_settings, pricing_pairs=pricing_pairs,
    )
    cache_ttl_case, cache_ttl_args = log_db._cache_ttl_known_case_sql(
        conn, model_expr=model_expr, channel_expr=channel_expr,
        cache_creation_expr="COALESCE(cache_creation_tokens, 0)",
        where_sql=standard_where, where_args=window_args,
        pricing_settings=pricing_settings, pricing_pairs=pricing_pairs,
    )
    rows = conn.execute(
        f"""SELECT {_bucket_expr()} AS b, {key_expr} AS k,
                   {model_expr} AS pricing_model,
                   {channel_expr} AS pricing_channel,
                   COALESCE(requested_model, '?') AS model_key,
                   COALESCE({pricing_exprs['fast_mode']}, 0) AS fast_mode,
                   {long_case} AS long_context,
                   {cache_ttl_case} AS cache_ttl_known,
                   MIN({pricing_exprs['usage_known']}) AS usage_semantics_known,
                   COUNT(*) AS row_count,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cache_creation_tokens) AS cache_creation_tokens,
                   SUM(cache_read_tokens) AS cache_read_tokens
              FROM request_log
             WHERE {standard_where}
             GROUP BY b, k, pricing_model, pricing_channel, model_key, fast_mode,
                      long_context, cache_ttl_known, {pricing_exprs['usage_known']}""",
        bucket_args + long_args + cache_ttl_args + window_args,
    ).fetchall()
    for row in rows:
        target = _target(out, row["b"], row["k"], count)
        if target is None:
            continue
        scratch = log_db._new_cost_agg()
        log_db._estimate_cost_into(
            scratch, row, row_count=int(row["row_count"] or 0),
            pricing_settings=pricing_settings,
        )
        add(target, scratch["cost_ticks"])
        target["unpriced_success"] += int(scratch["unpriced_success"] or 0)

    tables = log_db._existing_tables(conn)
    if "request_detail" in tables:
        detail_join = "LEFT JOIN request_detail USING (request_id)"
        response_expr = (
            f"substr(request_detail.response_body, -{log_db._XAI_COST_BODY_TAIL_CHARS})"
        )
    else:
        detail_join = ""
        response_expr = "NULL"
    xai_rows = conn.execute(
        f"""SELECT {_bucket_expr()} AS b, {key_expr} AS k,
                   request_log.status AS status,
                   COALESCE(request_log.final_model, request_log.requested_model, '?') AS pricing_model,
                   COALESCE(request_log.final_channel_key, '?') AS pricing_channel,
                   COALESCE(request_log.requested_model, '?') AS model_key,
                   COALESCE({pricing_exprs['fast_mode']}, 0) AS fast_mode,
                   request_log.input_tokens AS input_tokens,
                   request_log.output_tokens AS output_tokens,
                   request_log.cache_creation_tokens AS cache_creation_tokens,
                   request_log.cache_read_tokens AS cache_read_tokens,
                   {pricing_exprs['usage_known']} AS usage_semantics_known,
                   {response_expr} AS response_body
              FROM request_log
              {detail_join}
             WHERE {window}
               AND {xai_status} IN ('success','error','cancelled')
               {attempt_exclusion}
               AND COALESCE(request_log.final_channel_key, '') LIKE 'oauth:xai:%'""",
        bucket_args + window_args,
    )
    for row in xai_rows:
        target = _target(out, row["b"], row["k"], count)
        if target is None:
            continue
        normalized = model_pricing.normalize_response_billing(row["response_body"])
        if normalized.actual_cost_ticks is not None:
            add(target, normalized.actual_cost_ticks)
        elif normalized.usage_invalid:
            target["unpriced_success"] += 1
        elif row["status"] == "success":
            scratch = log_db._new_cost_agg()
            log_db._estimate_cost_into(
                scratch, row, row_count=1, pricing_settings=pricing_settings,
            )
            add(target, scratch["cost_ticks"])
            target["unpriced_success"] += int(scratch["unpriced_success"] or 0)
        elif row["response_body"]:
            target["unpriced_success"] += 1


def bucketed_stats(
    *, start: float, bucket_seconds: int, count: int, group: str | tuple[str, ...] | None,
    end: float | None = None,
) -> dict[tuple[int, str], dict[str, int]]:
    """Return ``{(bucket_index, group_key): raw additive point}`` for the window.

    ``group`` is one of ``None``, ``"model"``, ``"channel"``, ``"apikey"``, or a tuple of
    the named dimensions, whose keys are joined with :data:`KEY_SEPARATOR`.  ``end``
    clips the last bucket so the buckets add up to exactly ``[start, end)``.
    Only non-empty (bucket, key) pairs are present; callers fill gaps.
    """
    key_expr, attempt_key_expr, cost_attempt_key_expr, retry_key_expr = _group_exprs(group)
    full_end = float(start) + int(bucket_seconds) * int(count)
    end = full_end if end is None else min(full_end, float(end))
    out: dict[tuple[int, str], dict[str, int]] = {}
    if log_db._log_dir is not None and not os.path.isdir(log_db._log_dir):
        return out
    params = dict(
        start=float(start), end=end, bucket_seconds=int(bucket_seconds), count=int(count),
    )
    conns = log_db._iter_month_conns(float(start))
    try:
        for conn, _close in conns:
            if "request_log" not in log_db._existing_tables(conn):
                continue
            _accumulate_counts(conn, out, key_expr=key_expr, **params)
            _replace_tokens_with_attempts(
                conn, out, key_expr=key_expr, attempt_key_expr=attempt_key_expr, **params,
            )
            _accumulate_costs(
                conn, out, key_expr=key_expr,
                cost_attempt_key_expr=cost_attempt_key_expr, retry_key_expr=retry_key_expr, **params,
            )
    except sqlite3.Error as exc:
        raise log_db.HistoricalLogError(f"time-series aggregation failed: {exc}") from exc
    finally:
        for _conn, close in conns:
            try:
                close()
            except Exception:
                pass
    return out


_MONTH_FILE = re.compile(r"^\d{4}-\d{2}\.db$")


def earliest_request_at() -> float | None:
    """Creation time of the oldest request still in the logs (retention may have removed older ones)."""
    log_dir = log_db._log_dir
    if log_dir is None or not os.path.isdir(log_dir):
        return None
    current_month = log_db._current_db_path()[1]
    for name in sorted(n for n in os.listdir(log_dir) if _MONTH_FILE.match(n)):
        month = name[:-3]
        if month == current_month:
            conn, close = log_db._get_conn(), (lambda: None)
        else:
            conn = log_db._get_conn_for_month(month)
            if conn is None:
                continue
            close = conn.close
        try:
            if "request_log" not in log_db._existing_tables(conn):
                continue
            row = conn.execute("SELECT MIN(created_at) AS t FROM request_log").fetchone()
            if row is not None and row["t"] is not None:
                return float(row["t"])
        except sqlite3.Error as exc:
            raise log_db.HistoricalLogError(f"earliest request query failed: {exc}") from exc
        finally:
            close()
    return None


def recent_outcomes(
    *, since: float, group: str, per_key: int,
) -> dict[str, list[tuple[float, str]]]:
    """Return ``{group_key: [(created_at, status), ...]}`` oldest first, at most ``per_key`` each.

    Only finished requests (``success`` / ``error`` / ``cancelled``) newer than ``since`` count.
    """
    if group not in _GROUP_EXPRS or group is None:
        raise ValueError("unsupported outcome grouping")
    key_expr = _GROUP_EXPRS[group][0]
    out: dict[str, list[tuple[float, str]]] = {}
    if log_db._log_dir is not None and not os.path.isdir(log_db._log_dir):
        return out
    conns = log_db._iter_month_conns(float(since))
    try:
        for conn, _close in conns:
            if "request_log" not in log_db._existing_tables(conn):
                continue
            rows = conn.execute(
                f"""SELECT k, created_at, status FROM (
                        SELECT {key_expr} AS k, created_at, status,
                               ROW_NUMBER() OVER (PARTITION BY {key_expr} ORDER BY created_at DESC) AS rn
                          FROM request_log
                         WHERE created_at >= ? AND status IN ('success', 'error', 'cancelled'))
                     WHERE rn <= ?""",
                (float(since), int(per_key)),
            ).fetchall()
            for row in rows:
                out.setdefault(str(row["k"] or "?"), []).append((float(row["created_at"]), str(row["status"])))
    except sqlite3.Error as exc:
        raise log_db.HistoricalLogError(f"recent outcome query failed: {exc}") from exc
    finally:
        for _conn, close in conns:
            try:
                close()
            except Exception:
                pass
    for key, items in out.items():
        items.sort(key=lambda item: item[0])
        del items[:-int(per_key)]
    return out


def outcome_buckets(
    *, origin: float, bucket_seconds: int, count: int, group: str,
) -> dict[str, dict[int, dict[str, float]]]:
    """Return ``{group_key: {bucket_index: {total, errors, cancelled, last}}}`` of finished requests.

    Bucket ``i`` covers ``[origin + i * bucket_seconds, origin + (i + 1) * bucket_seconds)``.
    """
    if group not in _GROUP_EXPRS or group is None:
        raise ValueError("unsupported outcome grouping")
    key_expr = _GROUP_EXPRS[group][0]
    out: dict[str, dict[int, dict[str, float]]] = {}
    if log_db._log_dir is not None and not os.path.isdir(log_db._log_dir):
        return out
    conns = log_db._iter_month_conns(float(origin))
    try:
        for conn, _close in conns:
            if "request_log" not in log_db._existing_tables(conn):
                continue
            # "+status": keep SQLite on the created_at index for a short window (see bucketed_stats).
            rows = conn.execute(
                f"""SELECT {key_expr} AS k,
                           CAST((created_at - ?) / ? AS INTEGER) AS b,
                           COUNT(*) AS total,
                           SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,
                           SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS cancelled,
                           MAX(created_at) AS last
                      FROM request_log
                     WHERE created_at >= ? AND created_at < ?
                       AND +status IN ('success', 'error', 'cancelled')
                     GROUP BY k, b""",
                (float(origin), int(bucket_seconds), float(origin), float(origin) + int(bucket_seconds) * int(count)),
            ).fetchall()
            for row in rows:
                cell = out.setdefault(str(row["k"] or "?"), {}).setdefault(int(row["b"]), {"total": 0, "errors": 0, "cancelled": 0, "last": 0.0})
                cell["total"] += int(row["total"] or 0)
                cell["errors"] += int(row["errors"] or 0)
                cell["cancelled"] += int(row["cancelled"] or 0)
                cell["last"] = max(cell["last"], float(row["last"] or 0))
    except sqlite3.Error as exc:
        raise log_db.HistoricalLogError(f"outcome bucket query failed: {exc}") from exc
    finally:
        for _conn, close in conns:
            try:
                close()
            except Exception:
                pass
    return out
