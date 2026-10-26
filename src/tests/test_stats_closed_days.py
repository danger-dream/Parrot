"""Closed days of the live month: cached GROUP BY facts equal one full-window read."""
from __future__ import annotations

import dataclasses
import json
import random
from contextlib import closing
from datetime import datetime

import pytest

from src import log_db
from src.tests.test_proxy_stats_cache import BJT, _database, months  # noqa: F401  (fixture)


MONTH = datetime(2026, 9, 1, tzinfo=BJT).timestamp()
TODAY = datetime(2026, 9, 7, tzinfo=BJT).timestamp()
NOW = datetime(2026, 9, 7, 12, tzinfo=BJT).timestamp()


def _dump(value) -> str:
    return json.dumps(value, default=repr, ensure_ascii=False)


def _pick(values, index):
    return values[index % len(values)]


def _seed_month(conn, count: int = 150, *, mixed_tiers: bool = False) -> None:
    """Requests over the closed days and today: NULL / '' / '?' keys, failover
    attempts, unsettled dispatches (compact children too) and xAI bodies.
    ``mixed_tiers`` mixes priority-served requests into estimate groups."""
    span = NOW - MONTH - 600
    retry_id = 0
    for i in range(count):
        rid = f"r{i:03d}"
        created = MONTH + span * i / count
        channel = _pick([None, "?", "api:a", "oauth:claude:x", "oauth:xai:y", "api:b"], i)
        conn.execute(
            """INSERT INTO request_log(
                   request_id, created_at, api_key_name, requested_model, final_model,
                   final_channel_key, status, is_stream, input_tokens, output_tokens,
                   cache_creation_tokens, cache_read_tokens, connect_time_ms,
                   first_token_time_ms, total_time_ms, retry_count, affinity_hit,
                   upstream_protocol, usage_observed, fast_mode, actual_service_tier)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rid, created, _pick([None, "k1", "k2", ""], i),
                _pick([None, "?", "", "m-a", "m-b"], i), _pick([None, "m-a", "m-c"], i),
                channel, _pick(["success", "success", "error", "cancelled", "success"], i),
                i % 2, 100 + i, 10 + i % 17, (i % 4) * 30, (i % 3) * 50,
                _pick([None, 5, 7], i), _pick([None, 40, 60], i), 200 + i, i % 3, i % 2,
                _pick([None, "anthropic-messages", "openai-chat", "", "openai-responses"], i),
                _pick([1, None, 1, 0], i), int(i % 5 == 0),
                _pick([None, "priority", "default"], i) if mixed_tiers else None,
            ),
        )
        if channel == "oauth:xai:y":
            body = '{"usage":{"cost_in_usd_ticks":%d}}' % (1000 + i) if i % 2 else "not json"
            conn.execute(
                "INSERT INTO request_detail(request_id, response_body) VALUES(?,?)", (rid, body),
            )
        if i % 3 == 0:
            for order, attempt_channel in enumerate((channel or "api:a", "api:b")[: 1 + i % 2], start=1):
                retry_id += 1
                conn.execute(
                    """INSERT INTO retry_chain(
                           id, request_id, attempt_order, channel_key, channel_type, model,
                           started_at, dispatched_at, outcome, upstream_protocol)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (retry_id, rid, order, attempt_channel, _pick(["api", "oauth"], i), "m-a",
                     created, created, "success", _pick(["anthropic-messages", None], i)),
                )
                conn.execute(
                    """INSERT INTO upstream_attempt_usage(
                           retry_attempt_id, root_request_id, call_request_id, attempt_order,
                           channel_key, channel_type, model, outcome, usage_observed,
                           input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
                           service_tier, upstream_protocol, dispatch_state, cost_source,
                           cost_ticks, settled_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (retry_id, rid, rid, order, attempt_channel, "api", "m-a",
                     _pick(["error", "success"], order), 1, 50 + i, 5 + order, i % 7, i % 11,
                     _pick([None, "priority", "default"], i),
                     _pick(["anthropic-messages", "openai-chat"], i), "settled",
                     _pick(["actual", "estimated", "unpriced"], i), 300 + i, created),
                )
        if i % 7 == 0:
            for request_id in (rid, f"{rid}:compact:direct"):
                retry_id += 1
                conn.execute(
                    """INSERT INTO retry_chain(
                           id, request_id, attempt_order, channel_key, channel_type, model,
                           started_at, dispatched_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (retry_id, request_id, 9, _pick(["api:c", "api:a"], i), "api", "m-b",
                     created, created),
                )
    conn.commit()


@pytest.fixture
def priced(monkeypatch):
    settings = dataclasses.replace(log_db.model_pricing.settings(), enabled=True)
    monkeypatch.setattr(log_db.model_pricing, "settings", lambda *args, **kwargs: settings)


@pytest.fixture
def seeded(months, priced):
    log_db._reset_summary_cache_for_tests()
    log_db._reset_lifetime_cache_for_tests()
    with closing(_database(months.current)) as conn:
        _seed_month(conn)
    yield months
    log_db._reset_summary_cache_for_tests()
    log_db._reset_lifetime_cache_for_tests()


@pytest.fixture
def live(seeded, monkeypatch):
    monkeypatch.setenv("PARROT_STATS_CACHE", "1")
    return seeded


@pytest.fixture
def live_mixed(months, priced, monkeypatch):
    monkeypatch.setenv("PARROT_STATS_CACHE", "1")
    log_db._reset_summary_cache_for_tests()
    log_db._reset_lifetime_cache_for_tests()
    with closing(_database(months.current)) as conn:
        _seed_month(conn, mixed_tiers=True)
        # One estimate group across the boundary: fast_mode=0 rows served at mixed tiers.
        for rid, created, tier in (
            ("mix1", MONTH + 3600, "priority"), ("mix2", MONTH + 7200, None),
            ("mix3", TODAY + 60, "priority"), ("mix4", TODAY + 120, None),
        ):
            conn.execute(
                """INSERT INTO request_log(
                       request_id, created_at, api_key_name, requested_model, final_model,
                       final_channel_key, status, input_tokens, output_tokens,
                       upstream_protocol, usage_observed, fast_mode, actual_service_tier)
                   VALUES(?,?,'kz','m-z','m-z','api:z','success',1000,100,'openai-chat',1,0,?)""",
                (rid, created, tier),
            )
        conn.commit()
    yield months
    log_db._reset_summary_cache_for_tests()
    log_db._reset_lifetime_cache_for_tests()


def _uncached(monkeypatch, read):
    monkeypatch.setenv("PARROT_STATS_CACHE", "0")
    try:
        return read()
    finally:
        monkeypatch.setenv("PARROT_STATS_CACHE", "1")


def _summary_collections(monkeypatch) -> list:
    calls: list = []
    original = log_db._collect_summary_facts

    def counted(conn, since, until, include_cost):
        calls.append((since, until))
        return original(conn, since, until, include_cost)

    monkeypatch.setattr(log_db, "_collect_summary_facts", counted)
    return calls


@pytest.mark.parametrize("include_cost", [True, False])
def test_split_windows_merge_into_the_single_window_facts(seeded, include_cost):
    with closing(log_db._open_readonly(str(seeded.current))) as conn:
        full = log_db._collect_summary_facts(conn, MONTH, None, include_cost)
        assert full["groups"] and full["roots"]
        if include_cost:
            assert full["attempts"] and full["missing"] and full["estimates"]
        # The merge emits GROUP BY order; SQLite does too.
        for name, (keys, _sums) in log_db._SUMMARY_FACT_SPECS.items():
            rows = full[name]
            if rows is not None:
                assert rows == sorted(rows, key=lambda r: tuple(log_db._sqlite_sort_key(r[c]) for c in keys))
        random.seed(5)
        bounds = [TODAY, MONTH + 1, NOW - 1] + [random.uniform(MONTH, NOW) for _ in range(15)]
        for bound in bounds:
            merged = log_db._merge_summary_facts([
                log_db._collect_summary_facts(conn, MONTH, bound, include_cost),
                log_db._collect_summary_facts(conn, bound, None, include_cost),
            ])
            assert _dump(merged) == _dump(full), bound

        lifetime = log_db._lifetime_month_facts(conn)
        assert lifetime["costs"]["estimates"] and lifetime["costs"]["missing"]
        for bound in bounds:
            merged = log_db._merge_lifetime_facts([
                log_db._lifetime_month_facts(conn, 0.0, bound),
                log_db._lifetime_month_facts(conn, bound, None),
            ])
            assert _dump(merged) == _dump(lifetime), bound


def test_fine_estimate_groups_fold_into_the_original_groups(seeded):
    with closing(log_db._open_readonly(str(seeded.current))) as conn:
        original = log_db._usage_estimate_rows(conn, MONTH, None)
        fine = log_db._usage_estimate_rows(conn, MONTH, None, fine=True)
        assert original and len(fine) >= len(original)
        assert _dump(log_db._coarse_estimate_rows(fine, log_db._SUMMARY_ESTIMATE_KEYS)) == _dump(original)
        original = log_db._filtered_cost_facts(conn, 0, "1=1", ())["estimates"]
        fine = log_db._filtered_cost_facts(conn, 0, "1=1", (), fine=True)["estimates"]
        assert _dump(log_db._coarse_estimate_rows(fine, log_db._LIFETIME_ESTIMATE_KEYS)) == _dump(original)


def test_mixed_fast_groups_fall_back_to_the_original_estimate_query(live_mixed, monkeypatch):
    with closing(log_db._open_readonly(str(live_mixed.current))) as conn:
        fine = log_db._usage_estimate_rows(conn, MONTH, None, fine=True)
        assert log_db._coarse_estimate_rows(fine, log_db._SUMMARY_ESTIMATE_KEYS) is None
        fine = log_db._filtered_cost_facts(conn, 0, "1=1", (), fine=True)["estimates"]
        assert log_db._coarse_estimate_rows(fine, log_db._LIFETIME_ESTIMATE_KEYS) is None
    reference = _uncached(monkeypatch, lambda: log_db.stats_period_snapshot(MONTH))
    reference_lifetime = _uncached(monkeypatch, log_db.stats_lifetime)
    for _ in range(2):
        assert _dump(log_db.stats_period_snapshot(MONTH)) == _dump(reference)
        assert _dump(log_db.stats_lifetime()) == _dump(reference_lifetime)


def test_live_month_snapshot_reuses_its_closed_days(live, monkeypatch):
    reference = _uncached(monkeypatch, lambda: log_db.stats_period_snapshot(MONTH))
    reference_all = _uncached(monkeypatch, lambda: log_db.stats_period_snapshot(0.0))
    calls = _summary_collections(monkeypatch)
    first = log_db.stats_period_snapshot(MONTH)
    second = log_db.stats_period_snapshot(MONTH)
    assert _dump(first) == _dump(second) == _dump(reference)
    assert calls == [(MONTH, TODAY), (TODAY, None), (TODAY, None)]
    # The whole live file (lifetime-style breakdowns) keeps its own closed range.
    assert _dump(log_db.stats_period_snapshot(0.0)) == _dump(reference_all)
    assert calls[3:] == [(0.0, TODAY), (TODAY, None)]


def test_live_month_lifetime_reuses_its_closed_days(live, monkeypatch):
    reference = _uncached(monkeypatch, log_db.stats_lifetime)
    calls: list = []
    original = log_db._lifetime_month_facts

    def counted(conn, since_ts=0.0, until_ts=None):
        calls.append((since_ts, until_ts))
        return original(conn, since_ts, until_ts)

    monkeypatch.setattr(log_db, "_lifetime_month_facts", counted)
    assert _dump(log_db.stats_lifetime()) == _dump(reference)
    assert _dump(log_db.stats_lifetime()) == _dump(reference)
    assert calls == [(0.0, TODAY), (TODAY, None), (TODAY, None)]


@pytest.mark.parametrize("write", [
    "UPDATE request_log SET output_tokens=output_tokens+7, status='error' WHERE request_id='r010'",
    "DELETE FROM request_log WHERE request_id='r011'",
    """INSERT INTO upstream_attempt_usage(
           retry_attempt_id, root_request_id, call_request_id, attempt_order, channel_key,
           channel_type, model, outcome, usage_observed, input_tokens, output_tokens,
           cache_creation_tokens, cache_read_tokens, dispatch_state, cost_source, cost_ticks,
           settled_at)
       VALUES(9001, 'r012', 'r012', 5, 'api:z', 'api', 'm-a', 'success', 1, 9, 9, 0, 0,
              'settled', 'actual', 77, 0)""",
    """INSERT INTO retry_chain(id, request_id, attempt_order, channel_key, channel_type, model,
           started_at, dispatched_at)
       VALUES(9002, 'r013:compact:reduce', 7, 'api:z', 'api', 'm-a', 0, 0)""",
    "INSERT INTO request_log(request_id, created_at, status) VALUES('late', %r, 'success')" % (TODAY - 5),
])
def test_writes_to_closed_days_invalidate_the_cache(live, monkeypatch, write):
    log_db.stats_period_snapshot(MONTH)
    log_db.stats_lifetime()
    conn = log_db._get_conn()  # the request-log write connection (carries the triggers)
    with log_db._write_lock:
        conn.execute(write)
        conn.commit()
    reference = _uncached(monkeypatch, lambda: log_db.stats_period_snapshot(MONTH))
    reference_lifetime = _uncached(monkeypatch, log_db.stats_lifetime)
    calls = _summary_collections(monkeypatch)
    assert _dump(log_db.stats_period_snapshot(MONTH)) == _dump(reference)
    assert calls[0] == (MONTH, TODAY)
    assert _dump(log_db.stats_lifetime()) == _dump(reference_lifetime)


def test_writes_to_today_keep_the_closed_days(live, monkeypatch):
    log_db.stats_period_snapshot(MONTH)
    conn = log_db._get_conn()
    with log_db._write_lock:
        conn.execute("UPDATE request_log SET output_tokens=output_tokens+3 WHERE created_at>=?", (TODAY,))
        conn.execute(
            "INSERT INTO request_log(request_id, created_at, status) VALUES('new', ?, 'pending')", (NOW,),
        )
        conn.commit()
    reference = _uncached(monkeypatch, lambda: log_db.stats_period_snapshot(MONTH))
    calls = _summary_collections(monkeypatch)
    assert _dump(log_db.stats_period_snapshot(MONTH)) == _dump(reference)
    assert calls == [(TODAY, None)]


def test_untracked_changes_are_still_detected(live, monkeypatch):
    log_db.stats_period_snapshot(MONTH)
    # A connection without the triggers: row count / max id and maintenance epoch.
    with closing(_database(live.current)) as other:
        other.execute(
            "INSERT INTO request_log(request_id, created_at, status, output_tokens) VALUES('ext', ?, 'success', 5)",
            (MONTH + 60,),
        )
        other.commit()
    reference = _uncached(monkeypatch, lambda: log_db.stats_period_snapshot(MONTH))
    calls = _summary_collections(monkeypatch)
    assert _dump(log_db.stats_period_snapshot(MONTH)) == _dump(reference)
    assert calls[0] == (MONTH, TODAY)
    calls.clear()
    log_db._invalidate_summary_prefixes()
    log_db.stats_period_snapshot(MONTH)
    assert calls[0] == (MONTH, TODAY)
    calls.clear()
    monkeypatch.setattr(log_db, "_lifetime_pricing_signature", lambda: "changed pricing")
    log_db.stats_period_snapshot(MONTH)
    assert calls[0] == (MONTH, TODAY)


def test_closed_days_with_pending_requests_are_not_cached(live, monkeypatch):
    conn = log_db._get_conn()
    with log_db._write_lock:
        conn.execute(
            "INSERT INTO request_log(request_id, created_at, status) VALUES('slow', ?, 'pending')",
            (TODAY - 30,),
        )
        conn.commit()
    calls = _summary_collections(monkeypatch)
    log_db.stats_period_snapshot(MONTH)
    log_db.stats_period_snapshot(MONTH)
    assert calls == [(MONTH, None), (MONTH, None)]


def test_tracking_failures_never_break_a_log_write(live):
    log_db.stats_period_snapshot(MONTH)
    conn = log_db._get_conn()
    path = log_db._stats_path_key(str(live.current))
    broken = log_db.sqlite3.connect(":memory:", check_same_thread=False)
    broken.close()
    with log_db._stats_lookup_lock:
        log_db._close_stats_lookups_locked()
        log_db._stats_lookup_conns[path] = broken
    before = log_db._stats_write_generation.get(path, 0)
    with log_db._write_lock:
        conn.execute(
            """INSERT INTO retry_chain(id, request_id, attempt_order, channel_key, channel_type,
                   model, started_at) VALUES(9100, 'r020', 3, 'api:a', 'api', 'm-a', 0)""",
        )
        conn.commit()
    assert log_db._stats_write_generation.get(path, 0) > before  # unknown means invalidate
    with log_db._stats_lookup_lock:
        log_db._stats_lookup_conns.clear()
