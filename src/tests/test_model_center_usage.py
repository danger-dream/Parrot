"""Real temporary SQLite and the existing TG coordinator, never live I/O."""
from __future__ import annotations

import json
import sqlite3
import statistics
import threading
import time
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from src import log_db, model_mapping
from src.management_control.observability import telegram_context
from src.management_control.observability.stats import StatsControl
from src.telegram import menu_cache
from src.telegram.menus import model_center_usage as usage

BJT = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 16, 12, tzinfo=BJT)


def database(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(log_db._schema_sql())
    conn.commit()
    return conn


def root(conn, rid, *, channel="api:a", model="m", requested="request-alias",
         status="success", inp=0, out=0, cc=0, cr=0, stream=0, first=None, ms=None):
    conn.execute(
        """INSERT INTO request_log(request_id, created_at, final_channel_key,
           final_model, requested_model, status, input_tokens, output_tokens,
           cache_creation_tokens, cache_read_tokens, is_stream,
           first_token_time_ms, total_time_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rid, NOW.timestamp(), channel, model, requested, status, inp, out, cc, cr, stream, first, ms),
    )


def attempt(conn, rid, n, *, channel="api:a", model="m", inp=0, out=0,
            cc=0, cr=0, outcome="success", observed=1, call=None):
    conn.execute(
        """INSERT INTO upstream_attempt_usage(retry_attempt_id, root_request_id,
           call_request_id, attempt_order, channel_key, channel_type, model,
           outcome, usage_observed, input_tokens, output_tokens,
           cache_creation_tokens, cache_read_tokens, cost_source, settled_at)
           VALUES (?,?,?,?,?,'api',?,?,?,?,?,?,?,'unpriced',?)""",
        (n, rid, rid if call is None else call, n, channel, model, outcome, observed,
         inp, out, cc, cr, NOW.timestamp()),
    )


def source(id="api:a", outbound="m", kind="api", provider="openai"):
    return NS(type=kind, id=id, outbound_model=outbound, provider=provider)


def view(model="m", *sources):
    return NS(model_id=model, sources=sources, aliases=("request-alias", "another-alias"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(log_db, "datetime", Clock)
    monkeypatch.setattr(log_db, "_log_dir", str(logs))
    monkeypatch.setattr(log_db, "_local", threading.local())
    monkeypatch.setattr(log_db, "_retired_log_paths", set())
    registry = {}
    monkeypatch.setattr(log_db, "_write_conn_registry", registry)
    monkeypatch.setattr(log_db, "_model_center_usage_sealed", {})
    monkeypatch.setattr(log_db.model_pricing, "settings", lambda: NS(enabled=False))
    control = StatsControl()
    monkeypatch.setattr(menu_cache, "_STATS_CONTROL", control)
    monkeypatch.setattr(menu_cache, "DETAIL_STATS", menu_cache.SWRCache(300))
    worker = menu_cache.StatsRefreshCoordinator()
    monkeypatch.setattr(menu_cache, "COORDINATOR", worker)
    yield NS(logs=logs, old=logs / "2026-01.db", current=logs / "2026-09.db",
             control=control, worker=worker)
    worker.stop()
    for connections in registry.values():
        for conn in connections:
            conn.close()


def snapshot(env):
    return env.control.model_center_usage_snapshot(telegram_context())


def project(env, snap, views, selected=None):
    selections, channel = usage._selection(views, selected)
    return env.control.project_model_center_usage(snap, selections, channel_key=channel)


def seed_complex(env):
    with closing(database(env.old)) as conn:
        root(conn, "old", inp=20, out=100, cc=30, cr=50, stream=1, first=100, ms=1100)
        attempt(conn, "old", 1, channel="api:b", inp=10, out=20, cr=10, outcome="error")
        attempt(conn, "old", 2, inp=20, out=100, cc=30, cr=50)
        attempt(conn, "old", 3, inp=999, out=999, observed=0)
        attempt(conn, "old", 4, inp=999, out=999, call="")
        conn.execute("UPDATE request_log SET created_at=?", (datetime(2026, 1, 15, tzinfo=BJT).timestamp(),))
        conn.commit()
    with closing(database(env.current)) as conn:
        root(conn, "slow", channel="api:b", inp=70, out=300, cr=30, ms=6000)
        attempt(conn, "slow", 1, channel="api:b", inp=5, out=5, cr=5, outcome="error")
        root(conn, "failed", status="error")
        root(conn, "pending", status="pending")
        root(conn, "no-timing", inp=10, out=40, stream=1, ms=2000)
        root(conn, "removed-source", channel="api:removed", inp=10, cr=90, out=60, ms=3000)
        root(conn, "unattributed", channel=None, model=None, requested="m", status="pending")
        root(conn, "other-model", model="other", out=500, ms=500)
        attempt(conn, "other-model", 2, channel="api:c", model="attempt-only", inp=7, out=8, outcome="error")
        conn.commit()


def test_retained_lifetime_attempts_failures_pending_weighted_tps_and_format(env):
    seed_complex(env)
    snap = snapshot(env)
    views = [view("m", source(), source(), source("api:b")), view("unused")]
    result = project(env, snap, views)
    assert "unused" not in result
    assert result["m"] == {
        "total": 6, "success_count": 4, "error_count": 1,
        "input": 125, "output": 525, "cache_creation": 30, "cache_read": 185,
        "avg_tps": 46.0, "max_tps": 100.0, "min_tps": 20.0,
    }
    a = project(env, snap, views, source())["m"]
    b = project(env, snap, views, source("api:b"))["m"]
    assert (a["total"], a["output"], a["avg_tps"]) == (4, 140, 100)
    assert (b["total"], b["output"], b["avg_tps"]) == (1, 325, 50)
    for channel, metrics in (("api:a", a), ("api:b", b)):
        legacy = next(row for row in log_db.channel_model_stats(channel, 0) if row["final_model"] == "m")
        assert {key: legacy[key] for key in metrics} == metrics
    assert usage.format_lines(result["m"]) == [
        "💎 累计用量: ↑ 340 · ↓ 525 · 缓存 185 (54.4%)",
        "📨 请求：6 次 · 成功率 66.7% · 失败 1 次",
        "⚡️ TPS: 平均 46.0 t/s · 峰值 100 t/s · 最低 20.0 t/s",
    ]
    only = project(env, snap, [view("attempt-only", source("api:c", "attempt-only"))])["attempt-only"]
    assert only["total"] == 0 and only["input"] == 7 and only["avg_tps"] is None
    assert len(usage.format_lines(only)) == 2
    assert "N/A" in usage.format_lines(only)[1]
    assert usage.format_lines({}) == usage.format_lines(None) == []


def test_global_alias_api_public_real_and_oauth_provider_identity(env, monkeypatch):
    from src.channel.api_channel import ApiChannel
    from src.openai.channel.api_channel import OpenAIApiChannel

    monkeypatch.setattr(model_mapping, "get_ingress_map", lambda ingress: {"friendly": "public"})
    body = {"model": "friendly"}
    assert model_mapping.apply_mapping(body, "openai-chat") == ("friendly", "public")
    entry = {"name": "a", "models": [{"alias": "public", "real": "real"}]}
    for cls in (ApiChannel, OpenAIApiChannel):
        channel = cls(entry)
        assert channel.supports_model(body["model"]) == "real"
    with closing(database(env.current)) as conn:
        root(conn, "alias-call", requested="friendly", model="real", inp=10)
        root(conn, "unrelated", requested="public", model="different", inp=500)
        root(conn, "coincidental-id", requested="old-public", model="public", inp=999)
        root(conn, "cursor", channel="oauth:cursor:one", model="auto", out=30)
        root(conn, "wb", channel="oauth:workbuddy:two", model="auto", out=90)
        root(conn, "deleted-cursor", channel="oauth:cursor:deleted", model="auto", out=20)
        conn.commit()
    snap = snapshot(env)
    api = view("public", source(outbound="real"), source(outbound="real"))
    cursor = view("cursor-auto", source("cursor:one", "auto", "oauth", "cursor"))
    wb = view("workbuddy-auto", source("workbuddy:two", "auto", "oauth", "workbuddy"))
    result = project(env, snap, [api, cursor, wb, view("friendly"), view("auto")])
    assert set(result) == {"public", "cursor-auto", "workbuddy-auto"}
    assert (result["public"]["total"], result["public"]["input"]) == (1, 10)
    assert result["cursor-auto"]["output"] == 50 and result["workbuddy-auto"]["output"] == 90
    selected = project(env, snap, [cursor, wb], source("cursor:one", kind="oauth"))
    assert list(selected) == ["cursor-auto"] and selected["cursor-auto"]["output"] == 30
    api.sources = (source(outbound="different"),)
    assert project(env, snap, [api])["public"]["input"] == 500


def test_sealed_cache_late_write_removal_and_strict_source_enumeration(env, monkeypatch):
    seed_complex(env)
    calls = Counter()
    aggregate = log_db._aggregate_model_center_usage_month

    def counted(conn):
        calls[Path(conn.execute("PRAGMA database_list").fetchone()[2]).name] += 1
        return aggregate(conn)

    monkeypatch.setattr(log_db, "_aggregate_model_center_usage_month", counted)
    with closing(database(env.logs / "archived-copy.db")) as conn:
        root(conn, "copy", inp=999)
        conn.commit()
    first = snapshot(env)
    assert snapshot(env) == first
    assert calls == {"2026-01.db": 1, "2026-09.db": 2}
    first["routes"][("api:a", "m")]["total"] = -100
    assert snapshot(env)["routes"][("api:a", "m")]["total"] == 4
    with closing(sqlite3.connect(env.old)) as conn:
        root(conn, "late", inp=25)
        conn.commit()
    assert snapshot(env)["routes"][("api:a", "m")]["total"] == 5
    assert calls["2026-01.db"] == 2
    env.old.unlink()
    assert snapshot(env)["routes"][("api:a", "m")]["total"] == 3
    assert not log_db._model_center_usage_sealed


def test_historical_failure_is_not_partial_success(env):
    seed_complex(env)
    snapshot(env)
    with closing(sqlite3.connect(env.old)) as conn:
        conn.execute("DROP TABLE request_log")
        conn.commit()
    with pytest.raises(log_db.HistoricalLogError, match="2026-01"):
        snapshot(env)


def test_legacy_month_without_attempt_table_and_unmeasured_tps(env):
    with closing(database(env.old)) as conn:
        conn.execute("DROP TABLE upstream_attempt_usage")
        root(conn, "legacy", inp=10, out=20, cc=30, cr=40, stream=1, first=100, ms=100)
        conn.commit()
    row = project(env, snapshot(env), [view("m")])["m"]
    assert row["input"] == 10 and row["cache_read"] == 40
    assert row["avg_tps"] is row["max_tps"] is row["min_tps"] is None
    assert len(usage.format_lines(row)) == 2


def test_token_sum_overflow_is_exact(env):
    with closing(database(env.current)) as conn:
        root(conn, "large1", inp=2**62, cr=2**62)
        root(conn, "large2", inp=2**62, cr=2**62)
        conn.commit()
    row = project(env, snapshot(env), [view("m")])["m"]
    assert row["input"] == row["cache_read"] == 2**63


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    assert predicate()


def test_cold_hot_singleflight_callbacks_no_sql_and_failed_stale(env, monkeypatch):
    seed_complex(env)
    sql_threads, loads, callbacks = [], [], []
    original = log_db._aggregate_model_center_usage_month

    def measured(conn):
        conn.set_trace_callback(lambda sql: sql_threads.append(threading.get_ident()))
        return original(conn)

    monkeypatch.setattr(log_db, "_aggregate_model_center_usage_month", measured)
    load = env.control.model_center_usage_snapshot

    def loading(context):
        loads.append(threading.get_ident())
        return load(context)

    monkeypatch.setattr(env.control, "model_center_usage_snapshot", loading)
    views = [view("m", source())]
    assert usage.peek(views).value is None

    def completed(value, error):
        before = len(sql_threads)
        assert usage.peek(views).value["m"]["total"] == 6
        assert usage.request(views).fresh
        assert usage.format_lines(value["m"])
        callbacks.append((value, error, before, len(sql_threads), threading.get_ident()))

    for n in range(20):
        read = usage.request(views, source() if n % 2 else None, subscriber=n, on_ready=completed)
        assert read.value is None and read.refreshing
    assert not sql_threads and not loads
    assert len(env.worker._queue) == 1
    env.worker.start()
    wait_for(lambda: len(callbacks) == 20)
    assert len(loads) == 1 and loads[0] != threading.get_ident()
    assert set(sql_threads) == {env.worker.thread.ident}
    assert all(before == after and tid == env.worker.thread.ident for _, _, before, after, tid in callbacks)
    assert {row[0]["m"]["total"] for row in callbacks} == {4, 6}
    assert env.worker.max_active_jobs == 1
    before = len(sql_threads)
    for _ in range(30):
        assert usage.request(views).fresh
        usage.peek([view("other")], source())
    assert len(sql_threads) == before and len(loads) == 1

    cached = menu_cache.DETAIL_STATS.peek(usage._KEY).value
    menu_cache.DETAIL_STATS.store(usage._KEY, cached, age_seconds=301)
    failures = []

    def failing(_):
        raise RuntimeError("expected refresh failure")

    monkeypatch.setattr(env.control, "model_center_usage_snapshot", failing)
    stale = usage.request(views, subscriber="fail", on_ready=lambda v, e: failures.append((v, e)))
    assert stale.value["m"]["total"] == 6 and not stale.fresh
    wait_for(lambda: bool(failures))
    assert failures[0][0] is None and isinstance(failures[0][1], RuntimeError)
    stale = usage.peek(views)
    assert stale.value["m"]["total"] == 6 and stale.error and not stale.refreshing
    monkeypatch.setattr(env.control, "model_center_usage_snapshot", loading)
    usage.request(views)
    wait_for(lambda: usage.peek(views).fresh)
    assert usage.peek(views).error is None


def test_adapter_reuses_lifecycle_bound_stats_control(env, monkeypatch):
    assert not hasattr(usage, "_CONTROL")
    calls = []
    owner = StatsControl()
    monkeypatch.setattr(owner, "model_center_usage_snapshot", lambda context: calls.append(context) or {
        "routes": {}, "by_model": {},
    })
    monkeypatch.setattr(menu_cache, "_STATS_CONTROL", owner)
    usage.request([view("m")])
    assert not calls
    env.worker.start()
    wait_for(lambda: usage.peek([view("m")]).fresh)
    assert calls == [menu_cache._CONTEXT]
    assert usage.peek([view("m")]).value == {}


def test_replaced_subscriber_receives_only_latest_projection(env):
    seed_complex(env)
    results = []
    usage.request([view("m")], subscriber="same", on_ready=lambda v, e: results.append("old"))
    usage.request([view("other")], subscriber="same", on_ready=lambda v, e: results.append(v))
    env.worker.start()
    wait_for(lambda: results)
    assert len(results) == 1 and list(results[0]) == ["other"]


def test_query_count_independent_of_model_count_and_hot_projection(env, monkeypatch):
    grouped_counts = []
    original = log_db._aggregate_model_center_usage_month
    queries = []

    def traced(conn):
        conn.set_trace_callback(lambda sql: queries.append(sql))
        return original(conn)

    monkeypatch.setattr(log_db, "_aggregate_model_center_usage_month", traced)
    for models in (1, 150):
        with closing(database(env.current)) as conn:
            conn.execute("DELETE FROM upstream_attempt_usage")
            conn.execute("DELETE FROM request_log")
            for i in range(600):
                root(conn, str(i), model=f"m{i % models}", channel=f"api:{i % 8}", inp=10)
                attempt(conn, str(i), i + 1, model=f"m{i % models}", channel=f"api:{i % 8}", inp=10)
            conn.commit()
        queries.clear()
        snap = snapshot(env)
        grouped_counts.append(sum("GROUP BY" in sql for sql in queries))
        before = len(queries)
        menu_cache.DETAIL_STATS.store(usage._KEY, snap)
        assert len(usage.request([view(f"m{i}") for i in range(models)]).value) == models
        assert len(queries) == before
    assert grouped_counts == [3, 3]


def test_simultaneous_request_reservations_are_singleflight(env, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    seed_complex(env)
    barrier = threading.Barrier(16)
    loads = []
    original = env.control.model_center_usage_snapshot

    def load(context):
        loads.append(threading.get_ident())
        return original(context)

    monkeypatch.setattr(env.control, "model_center_usage_snapshot", load)

    def request_one(i):
        barrier.wait(timeout=5)
        return usage.request([view("m" if i % 2 else "other")], subscriber=i)

    with ThreadPoolExecutor(max_workers=16) as executor:
        reads = list(executor.map(request_one, range(16)))
    assert all(read.value is None and read.refreshing for read in reads)
    assert len(env.worker._queue) == 1 and not loads
    env.worker.start()
    wait_for(lambda: usage.peek([view("m")]).fresh)
    assert len(loads) == 1 and env.worker.max_active_jobs == 1


def test_reproducible_performance_sample(env, monkeypatch):
    roots_per_month = 40_000
    for month in ("2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"):
        month_ts = datetime.strptime(month, "%Y-%m").replace(tzinfo=BJT).timestamp()
        with closing(database(env.logs / f"{month}.db")) as conn:
            conn.executemany(
                """INSERT INTO request_log(request_id,created_at,final_channel_key,
                   final_model,status,input_tokens,output_tokens,cache_creation_tokens,
                   cache_read_tokens,is_stream,total_time_ms) VALUES (?,?,?,?,?,?,?,?,?,0,?)""",
                ((f"{month}-r{i}", month_ts, f"api:{(i // 120) % 12}", f"m{i % 120}",
                  "error" if i % 25 == 0 else "success", 100, 60, 20, 880, 1500 + i % 1000)
                 for i in range(roots_per_month)),
            )
            conn.executemany(
                """INSERT INTO upstream_attempt_usage(retry_attempt_id,root_request_id,
                   call_request_id,attempt_order,channel_key,channel_type,model,outcome,
                   usage_observed,input_tokens,output_tokens,cache_creation_tokens,
                   cache_read_tokens,cost_source,settled_at)
                   VALUES (?,?,?,1,?,'api',?,'success',1,100,60,20,880,'unpriced',?)""",
                ((i + 1, f"{month}-r{i}", f"{month}-r{i}", f"api:{(i // 120) % 12}", f"m{i % 120}", month_ts)
                 for i in range(0, roots_per_month, 4)),
            )
            conn.commit()
    calls = Counter()
    queries = []
    original = log_db._aggregate_model_center_usage_month

    def counted(conn):
        calls[Path(conn.execute("PRAGMA database_list").fetchone()[2]).name] += 1
        conn.set_trace_callback(queries.append)
        return original(conn)

    monkeypatch.setattr(log_db, "_aggregate_model_center_usage_month", counted)
    started = time.perf_counter()
    snap = snapshot(env)
    cold_ms = (time.perf_counter() - started) * 1000
    assert sum(row["total"] for row in snap["routes"].values()) == 240_000
    assert len(calls) == 6 and set(calls.values()) == {1}
    cold_group_queries = sum("GROUP BY" in sql for sql in queries)
    assert cold_group_queries == 18
    started = time.perf_counter()
    assert snapshot(env) == snap
    sealed_reuse_ms = (time.perf_counter() - started) * 1000
    assert calls["2026-09.db"] == 2 and sum(calls.values()) == 7
    menu_cache.DETAIL_STATS.store(usage._KEY, snap)
    hot = []
    before_hot_queries = len(queries)
    for i in range(200):
        views = [view(f"m{(i + j) % 120}") for j in range(8)]
        started = time.perf_counter()
        read = usage.request(views, source("api:3") if i % 2 else None)
        for metrics in read.value.values():
            usage.format_lines(metrics)
        hot.append((time.perf_counter() - started) * 1000)
    assert sum(calls.values()) == 7 and len(queries) == before_hot_queries
    print("MODEL_USAGE_PERF " + json.dumps({
        "roots": 240_000, "attempts": 60_000, "months": 6, "models": 120, "sources": 12,
        "sqlite_bytes": sum(path.stat().st_size for path in env.logs.glob("*.db")),
        "cold_ms": round(cold_ms, 2), "sealed_reuse_ms": round(sealed_reuse_ms, 2),
        "hot_8_rows_median_ms": round(statistics.median(hot), 3),
        "hot_8_rows_p95_ms": round(sorted(hot)[189], 3), "hot_sql_queries": 0,
        "cold_group_queries": cold_group_queries,
    }, sort_keys=True))
