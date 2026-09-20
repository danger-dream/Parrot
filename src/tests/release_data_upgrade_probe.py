"""Subprocess-only probe used by test_release_data_upgrade (never contacts upstream)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import time


def no_network(*args, **kwargs):
    raise AssertionError("release upgrade probe must not access any network")


# Installed before importing either release. No credentials or production paths.
socket.socket.connect = no_network
socket.socket.connect_ex = no_network
socket.create_connection = no_network
socket.getaddrinfo = no_network
MODE, ROOT = sys.argv[1], Path(sys.argv[2]).resolve()
assert Path(os.environ["ANTHROPIC_PROXY_DATA_DIR"]).resolve() == ROOT
assert Path(os.environ["ANTHROPIC_PROXY_CONFIG"]).resolve().is_relative_to(ROOT)

from src import config, image_db, log_db, state_db  # noqa: E402

BJT = timezone(timedelta(hours=8))
CHANNEL = "api:upgrade-isolated"
MODEL = "gpt-4o"
BASELINE = ROOT / "baseline.json"


def init():
    # Run the real config normalizer for the version, but never DEFAULT_CONFIG fixtures.
    cfg = config.get()
    for key in ("stateDbPath", "runtimeStatePath", "durableStatePath", "logDir"):
        assert Path(cfg[key]).resolve().is_relative_to(ROOT)
    assert Path(cfg["images"]["dbPath"]).resolve().is_relative_to(ROOT)
    state_db.init()
    log_db.init()
    image_db.init()


def request(request_id, timestamp, response_model=MODEL):
    handle = log_db.insert_pending(
        request_id, "127.0.0.1", "fixture-client", MODEL, False, 1, 0,
        {"x-fixture": "old-writer"}, {"messages": [{"role": "user", "content": request_id}]},
        ingress_protocol="openai-chat", created_at=timestamp,
    )
    log_db.record_retry_attempt(handle, 1, CHANNEL, "api", MODEL, started_at=timestamp)
    web = log_db.record_local_web_call(handle, 1, "WebSearch", query="offline-fixture",
                                       started_at=timestamp)
    log_db.finish_local_web_call(web, status="success", result_count=2, ended_at=timestamp + 1)
    log_db.finish_success(
        handle, CHANNEL, "api", MODEL, input_tokens=101, output_tokens=17,
        cache_read_tokens=23, total_ms=1200, usage_observed=True,
        upstream_protocol="openai-chat", response_body=json.dumps({
            "model": response_model, "usage": {"prompt_tokens": 101, "completion_tokens": 17,
                                       "prompt_tokens_details": {"cached_tokens": 23}},
            "choices": [{"message": {"content": "fixture response"}}],
        }),
    )


def image(request_id):
    row_id = image_db.start_call(
        request_id=request_id, api_key_name="fixture-client", action="generate",
        main_model="gpt-5", tool_model="gpt-image-1", size="1024x1024",
        prompt_preview="offline image", prompt_hash="isolated-hash",
    )
    attempt = image_db.start_attempt(row_id, request_id=request_id,
                                    account_key="isolated-account", account_email="fixture@example.test")
    image_db.finish_attempt(attempt, status="success", duration_ms=500,
                            image_count=1, image_bytes=1234)
    image_db.finish_call(row_id, status="success", duration_ms=500, image_count=1,
                         cached_images=1, image_bytes=1234,
                         cache_paths=["fixture.png"], usage={"cost_in_usd_ticks": 100000})
    return row_id


def contents():
    out = {}
    for path in sorted((ROOT / "logs").glob("*.db")) + [ROOT / "image_logs.db"]:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            out[str(path.relative_to(ROOT))] = {
                table: [dict(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
                for table in tables
            }
    return out


def schemas():
    result = {}
    for path in sorted((ROOT / "logs").glob("*.db")) + [ROOT / "image_logs.db"]:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as conn:
            result[path.name] = conn.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    return result


def snapshots():
    return {kind: json.loads((ROOT / filename).read_text())["payload"]
            for kind, filename in (("runtime", "runtime-cache.json"), ("durable", "durable-state.json"))}


def counts():
    return {"request_count": log_db.management_logs_count(),
            "media_count": len(image_db.media_recent(limit=100))}


def assert_old_sql(baseline):
    current = contents()
    for db, tables in baseline["sql"].items():
        for table, rows in tables.items():
            assert len(current[db][table]) >= len(rows), (db, table)
            for index, row in enumerate(rows):
                found = current[db][table][index]
                assert {key: found[key] for key in row} == row, (db, table, row, found)


def assert_old_state(baseline):
    for kind, domains in baseline["snapshots"].items():
        for domain, rows in domains.items():
            assert state_db.get_store().items(domain) == rows, (kind, domain)
    assert state_db.xai_video_job_load("old-video-job")["channel_key"] == CHANNEL
    assert state_db.xai_video_job_load("old-video-job").get("state_key") is None
    assert state_db.affinity_load("old-fingerprint")["prompt_cache_key"] == "old-cache-key"
    assert state_db.perf_load(CHANNEL, MODEL)["total_requests"] == 7
    assert state_db.updater_load()["stage"] == "idle"


def close():
    state_db.close()
    image_db.checkpoint()
    image_db._conn.close()
    for connections in log_db._write_conn_registry.values():
        for conn in connections:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()


def seed():
    init()
    month = datetime.now(BJT).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous = month - timedelta(days=1)
    older = previous.replace(day=1) - timedelta(days=1)
    for name, timestamp in (("old-current", time.time()),
                            ("old-previous", previous.timestamp()), ("old-older", older.timestamp())):
        request(name, timestamp)
    image_id = image("old-image")
    ids = {}
    for status in ("pending", "success"):
        video = image_db.start_media_call(
            request_id="old-video-" + status, api_key_name="fixture-client", provider="xai",
            media_type="video", action="generate", model="grok-imagine-video",
            prompt="offline video", media_duration_seconds=6,
        )
        image_db.finish_media_call(
            video, status=status, upstream_request_id="upstream-" + status,
            progress=50 if status == "pending" else 100,
            image_count=0 if status == "pending" else 1,
            usage=None if status == "pending" else {"cost_in_usd_ticks": 200000},
        )
        ids[status] = video
    state_db.xai_video_job_save("old-video-job", channel_key=CHANNEL,
                              api_key_name="fixture-client", model="grok-imagine-video", ttl_seconds=86400)
    state_db.affinity_upsert("old-fingerprint", CHANNEL, MODEL, prompt_cache_key="old-cache-key")
    state_db.perf_save(CHANNEL, MODEL, {"total_requests": 7, "success_count": 6})
    state_db.network_check_save({"key": "offline", "ok": True, "detail": "fixture"})
    state_db.updater_save({"stage": "idle", "target_version": "v0.32.2"})
    state_db.status_seen_mark("fixture", "update-1", "incident-1", "resolved")
    state_db.flush(strict=True)
    result = counts()
    baseline = {"sql": contents(), "snapshots": snapshots(),
                "months": [older.strftime("%Y-%m"), previous.strftime("%Y-%m")],
                "video_ids": ids, "image_id": image_id}
    BASELINE.write_text(json.dumps(baseline, ensure_ascii=False, indent=2))
    result["snapshot_version"] = json.loads((ROOT / "durable-state.json").read_text())["version"]
    result["months"] = baseline["months"]
    close()
    return result


def upgrade():
    baseline = json.loads(BASELINE.read_text())
    hashes = {month: hashlib.sha256((ROOT / "logs" / (month + ".db")).read_bytes()).hexdigest()
              for month in baseline["months"]}
    init()
    assert_old_sql(baseline)
    assert_old_state(baseline)
    assert state_db.get_store().items("model_reroute_mutes") == {}
    rows, total = log_db.management_logs_page(page=1, page_size=20)
    assert total == 3 and {row["request_id"] for row in rows} == {"old-current", "old-previous", "old-older"}
    assert log_db.management_logs_count(query="old-") == 3
    for request_id in ("old-current", "old-previous", "old-older"):
        detail = log_db.management_log_detail(request_id)
        assert detail["log"]["input_tokens"] == 101
        assert len(detail["retry_chain"]) == len(detail["billing_attempts"]) == len(detail["local_web_log"]) == 1
        assert detail["billing_attempts"][0]["output_tokens"] == 17
    summary = log_db.stats_summary(0, include_cost=False)
    assert summary["overall"]["total"] == 3, summary
    channel_stats = log_db.channel_model_stats(CHANNEL, 0)
    assert channel_stats and sum(row["total"] for row in channel_stats) == 3, channel_stats
    assert log_db.search_call_stats(0) == []
    assert log_db.mcp_call_stats(0) == []
    assert image_db.model_statistics("image")[0]["generated_count"] == 1
    assert image_db.model_statistics("video")[0]["generated_count"] == 1
    assert hashes == {month: hashlib.sha256((ROOT / "logs" / (month + ".db")).read_bytes()).hexdigest()
                      for month in hashes}
    # Explicitly migrate sealed months only after proving normal queries are read-only.
    for month in baseline["months"]:
        log_db.migrate_month_schema(month)
    first_schema = schemas()
    for month in baseline["months"]:
        log_db.migrate_month_schema(month)
    image_db._conn.close()
    image_db._conn = None
    image_db.init()
    state_db.close()
    state_db.init()
    assert schemas() == first_schema
    assert_old_sql(baseline)
    assert_old_state(baseline)
    for path in (ROOT / "logs").glob("*.db"):
        with sqlite3.connect(path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(request_log)")}
            assert {"upstream_actual_model", "model_signal_conflict", "safety_review"} <= columns
            assert {"search_call_log", "mcp_call_log", "mcp_call_detail"} <= {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"idx_search_call_source", "idx_mcp_call_id", "idx_mcp_call_detail_started"} <= {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    media_conn = image_db._get_conn()
    assert {"cache_status", "cache_error_class", "output_sizes"} <= {
        row[1] for row in media_conn.execute("PRAGMA table_info(image_call_logs)")}
    assert {"idx_media_provider_type", "idx_media_updated", "idx_media_upstream_request"} <= {
        row[0] for row in media_conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    request("new-current", time.time(), response_model="gpt-4o-2024-08-06")
    assert log_db.log_detail("new-current")["log"]["upstream_actual_model"] == "gpt-4o-2024-08-06"
    # New top-level tables can be written in migrated historical months without
    # reinterpreting legacy local_web_log as a second billed search call.
    previous_ts = datetime.strptime(baseline["months"][-1], "%Y-%m").replace(tzinfo=BJT).timestamp()
    search = log_db.record_search_call(call_id="new-search", source_id="fixture-search",
                                      source_type="exa", started_at=previous_ts)
    log_db.finish_search_call(search, status="success", result_count=1, provider="exa",
                              response_body={"costDollars": {"total": 0.00001}})
    mcp = log_db.record_mcp_call(call_id="new-mcp", tool_name="search", started_at=previous_ts)
    log_db.finish_mcp_call(mcp, status="success", result_count=1, cost_ticks=100000)
    log_db.save_mcp_call_detail(mcp, {"content": "offline result"}, created_at=previous_ts)
    assert_new_calls()
    new_image = image_db.start_media_call(
        request_id="new-image", api_key_name="fixture-client", provider="xai",
        media_type="image", action="generate", model="grok-imagine-image",
    )
    image_db.finish_media_call(new_image, status="success", image_count=2,
                               output_sizes=["1024x1024", "1024x1024"], cache_status="cached",
                               usage={"parrot_usage_by_call": [{"cost_in_usd_ticks": 10}, {"cost_in_usd_ticks": 20}]})
    assert image_db.get_log(new_image)["cost_usd_ticks"] == 30
    state_db.xai_video_job_save("new-video-job", channel_key=CHANNEL, api_key_name="fixture-client",
                              model="grok-imagine-video", ttl_seconds=86400, state_key="isolated-account")
    state_db.get_store()._mutate("model_reroute_mutes", lambda d: d.__setitem__("fixture-mute", {"until": 9999999999}))
    result = counts()
    result.update(old_rows_preserved=True, readonly_history_unchanged=True, idempotent_schema=True,
                  summary=summary["overall"], channel_stats=channel_stats)
    close()
    return result


def assert_new_calls():
    stats = log_db.search_call_stats(0)
    assert len(stats) == 1 and stats[0]["attempts"] == 1 and stats[0]["cost_ticks"] == 100000
    assert len(log_db.mcp_call_stats(0)) == 1
    detail = log_db.mcp_call_detail("new-mcp")
    assert detail and "offline result" in detail["result_body"]


def restart():
    before = schemas()
    init()
    assert before == schemas()
    assert_new_calls()
    assert_old_sql(json.loads(BASELINE.read_text()))
    result = counts()
    result.update(new_state_key=state_db.xai_video_job_load("new-video-job")["state_key"],
                  mute_present=bool(state_db.get_store().get("model_reroute_mutes", "fixture-mute")))
    close()
    return result


def rollback():
    assert "fixture-mute" in snapshots()["durable"]["model_reroute_mutes"]
    init()
    result = counts()
    assert result == {"request_count": 4, "media_count": 4}
    rows = image_db.media_recent(limit=100)
    new = next(row for row in rows if row["request_id"] == "new-image")
    assert new["cache_status"] == "cached"
    assert json.loads(new["output_sizes"]) == ["1024x1024", "1024x1024"]
    assert log_db.log_detail("new-current")["log"]["upstream_actual_model"] == "gpt-4o-2024-08-06"
    result["new_sql_fields_survive"] = True
    result["new_state_key_readable"] = state_db.xai_video_job_load("new-video-job")["state_key"] == "isolated-account"
    request("rollback-current", time.time())
    image("rollback-image")
    # A real old-version durable write drops domains it does not recognize.
    state_db.updater_save({"stage": "idle", "rollback_fixture": True})
    state_db.affinity_upsert("rollback-fingerprint", CHANNEL, MODEL)
    close()
    result["unknown_domain_dropped_on_write"] = "model_reroute_mutes" not in snapshots()["durable"]
    return result


def round_trip():
    init()
    assert_new_calls()
    assert_old_sql(json.loads(BASELINE.read_text()))
    result = counts()
    result["rollback_write_readable"] = bool(state_db.updater_load()["rollback_fixture"] and
                                             state_db.affinity_load("rollback-fingerprint"))
    result["mute_absent_after_rollback"] = not state_db.get_store().get("model_reroute_mutes", "fixture-mute")
    close()
    return result


def resume_video():
    baseline = json.loads(BASELINE.read_text())
    init()
    assert_old_sql(baseline)
    video_id = baseline["video_ids"]["pending"]
    assert image_db.get_log(video_id)["status"] == "pending"
    assert image_db.update_media_job(
        "upstream-pending", status="success", upstream_status="done", progress=100,
        image_count=1, usage={"cost_in_usd_ticks": 70}, cache_paths=["fixture.mp4"],
        cache_status="cached", output_sizes=["1280x720"],
    )
    completed = image_db.get_log(video_id)
    assert completed["status"] == "success" and completed["cost_usd_ticks"] == 70
    assert completed["finished_at"] is not None
    image_db.update_media_job("upstream-pending", status="pending", progress=10,
                              usage={"cost_in_usd_ticks": 0}, cache_paths=[])
    assert image_db.get_log(video_id) == completed
    (ROOT / "completed-video.json").write_text(json.dumps(completed))
    result = counts()
    result["old_video_resumed_in_place"] = True
    close()
    return result


def restart_video():
    init()
    completed = json.loads((ROOT / "completed-video.json").read_text())
    loaded = image_db.get_log(completed["id"])
    assert loaded == completed, {key: (completed[key], loaded[key]) for key in completed if completed[key] != loaded[key]}
    result = counts()
    result["resumed_video_preserved"] = True
    close()
    return result


if __name__ == "__main__":
    result = {"seed": seed, "upgrade": upgrade, "restart": restart,
              "rollback": rollback, "round_trip": round_trip,
              "resume_video": resume_video, "restart_video": restart_video}[MODE]()
    print("UPGRADE_RESULT=" + json.dumps(result, ensure_ascii=False, sort_keys=True))
