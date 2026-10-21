"""正文保留（logBodyRetention）：只清理完整正文、元数据永久保留、分批/续做/压缩契约。"""

from __future__ import annotations

# 必须在任意 src 业务模块导入前安装测试隔离。
import os as _ap_os
import sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src import config, log_body_retention as body, log_db
from src.management_auth import AuthMethod, ManagementPrincipal
from src.management_control import ManagementContext, ManagementError, ManagementErrorCode
from src.management_control.observability import LogBodyKind, RetentionControl, RetentionMode
from src.management_control.observability.logs import LogsControl
from src.management_api.schemas.observability import RequestLogDetailData, RetentionSettingsData


_BJT = timezone(timedelta(hours=8))


def _ts(year: int, month: int, day: int, hour: int = 0) -> float:
    return datetime(year, month, day, hour, tzinfo=_BJT).timestamp()


def _set_body_policy(value):
    config.update(lambda cfg: cfg.__setitem__("logBodyRetention", value))


@pytest.fixture
def body_log_dir(tmp_path, monkeypatch):
    local = threading.local()
    registry: dict[str, list[sqlite3.Connection]] = {}
    monkeypatch.setattr(log_db, "_log_dir", str(tmp_path))
    monkeypatch.setattr(log_db, "_local", local)
    monkeypatch.setattr(log_db, "_write_conn_registry", registry)
    monkeypatch.setattr(log_db, "_retired_log_paths", set())
    monkeypatch.setattr(log_db, "_last_retention_cleanup_key", None)
    monkeypatch.setattr(body, "BATCH_PAUSE_SECONDS", 0)
    log_db._request_handles.clear()
    body._reset_for_tests()
    config.update(lambda cfg: cfg.__setitem__("logStoreBodies", True))
    _set_body_policy({"mode": "days", "days": 7})
    try:
        yield tmp_path
    finally:
        body._reset_for_tests()
        _set_body_policy({"mode": "forever", "days": None})
        for conns in registry.values():
            for conn in conns:
                try:
                    conn.close()
                except Exception:
                    pass
        for conn in getattr(local, "write_conns", {}).values():
            try:
                conn.close()
            except Exception:
                pass
        log_db._request_handles.clear()


def _write_request(request_id: str, created_at: float, *, body_text: str = "payload", finish: bool = True):
    handle = log_db.insert_pending(
        request_id, "127.0.0.1", "test-key", "test-model", True, 1, 0,
        {"x-test": "yes"}, {"messages": [{"role": "user", "content": body_text}]},
        created_at=created_at,
    )
    retry = log_db.record_retry_attempt(handle, 1, "api:test", "api", "test-model", created_at)
    log_db.mark_retry_attempt_dispatch(retry, {})
    log_db.update_retry_attempt(
        retry, outcome="success",
        response_body='{"usage":{"input_tokens":3,"output_tokens":2}}', ended_at=created_at + 1,
    )
    if finish:
        log_db.finish_success(handle, "api:test", "api", "test-model", response_body=f"response:{body_text}")
    return handle


def _rows(path, sql, args=()):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _metadata_counts(path) -> dict[str, int]:
    return {
        table: _rows(path, f"SELECT COUNT(*) FROM {table}")[0][0]
        for table in ("request_log", "request_detail", "retry_chain", "upstream_attempt_usage")
    }


def _wait_idle(timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    while body.body_cleanup_busy():
        assert time.time() < deadline, "body purge runner did not finish"
        time.sleep(0.02)
    return body.body_retention_status()


def test_purge_clears_only_expired_bodies_and_keeps_all_metadata(body_log_dir):
    _write_request("old-1", _ts(2026, 2, 2))
    _write_request("old-2", _ts(2026, 2, 3))
    _write_request("fresh", _ts(2026, 2, 14))
    config.update(lambda cfg: cfg.__setitem__("logStoreBodies", False))
    _write_request("old-unstored", _ts(2026, 2, 4))
    config.update(lambda cfg: cfg.__setitem__("logStoreBodies", True))
    path = body_log_dir / "2026-02.db"
    before = _metadata_counts(path)
    usage_sql = (
        "SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), "
        "SUM(COALESCE(cost_ticks, 0)), GROUP_CONCAT(cost_source) FROM upstream_attempt_usage"
    )
    usage_before = _rows(path, usage_sql)
    lifetime_before = log_db.stats_lifetime()

    summary = body.run_body_purge(_ts(2026, 2, 8))
    assert summary["ok"] is True, summary
    assert summary["purged_requests"] == 2
    assert _metadata_counts(path) == before
    rows = dict(
        (rid, (headers, req, resp, purged))
        for rid, headers, req, resp, purged in _rows(
            path, "SELECT request_id, request_headers, request_body, response_body, purged_at FROM request_detail",
        )
    )
    for rid in ("old-1", "old-2"):
        assert rows[rid][:3] == (None, None, None)
        assert rows[rid][3] is not None
    assert rows["fresh"][1] and rows["fresh"][2] and rows["fresh"][3] is None
    # Never-stored bodies are not relabelled as "purged by policy".
    assert rows["old-unstored"] == (None, None, None, None)
    # Statistics and cost facts are untouched by body purging.
    assert _rows(path, usage_sql) == usage_before
    assert log_db.stats_lifetime() == lifetime_before
    assert lifetime_before["total"] == 4


def test_purge_skips_in_flight_requests_and_resumes_after_they_finish(body_log_dir):
    _write_request("old-done", _ts(2026, 2, 2))
    active = _write_request("old-active", _ts(2026, 2, 1), finish=False)
    path = body_log_dir / "2026-02.db"

    first = body.run_body_purge(_ts(2026, 2, 8))
    assert first["purged_requests"] == 1
    assert first["skipped_active"] == 1
    mark = _rows(path, "SELECT value FROM parrot_retention_state WHERE key='request_body_purged_through'")
    # The watermark stays before the skipped request so it is revisited.
    assert mark and mark[0][0] <= _ts(2026, 2, 1)
    log_db.finish_success(active, "api:test", "api", "test-model", response_body="late")
    second = body.run_body_purge(_ts(2026, 2, 8))
    assert second["purged_requests"] == 1
    assert _rows(path, "SELECT COUNT(*) FROM request_detail WHERE purged_at IS NOT NULL")[0][0] == 2
    mark = _rows(path, "SELECT value FROM parrot_retention_state WHERE key='request_body_purged_through'")
    assert mark[0][0] == pytest.approx(_ts(2026, 2, 8))


def test_batches_release_the_write_lock_and_resume_from_watermark(body_log_dir, monkeypatch):
    for index in range(7):
        _write_request(f"old-{index}", _ts(2026, 2, 2) + index)
    path = body_log_dir / "2026-02.db"
    monkeypatch.setattr(body, "BATCH_ROWS", 2)
    monkeypatch.setattr(body, "BATCH_PAUSE_SECONDS", 0.001)
    observed: list[bool] = []
    real_sleep = body.time.sleep

    def checking_sleep(seconds):
        # Another thread must be able to take the log write lock between batches.
        got: list[bool] = []
        probe = threading.Thread(target=lambda: got.append(log_db._write_lock.acquire(timeout=1)) or (
            got[-1] and log_db._write_lock.release()
        ))
        probe.start()
        probe.join()
        observed.append(bool(got and got[0]))
        if len(observed) == 2:
            body._stop_event.set()  # simulate a restart after two batches
        real_sleep(seconds)

    monkeypatch.setattr(body.time, "sleep", checking_sleep)
    interrupted = body.run_body_purge(_ts(2026, 2, 8))
    assert interrupted["stopped"]
    assert observed == [True, True]
    assert _rows(path, "SELECT COUNT(*) FROM request_detail WHERE purged_at IS NOT NULL")[0][0] == 4

    body._stop_event.clear()
    resumed = body.run_body_purge(_ts(2026, 2, 8))
    assert resumed["ok"] is True
    assert resumed["purged_requests"] == 3
    assert _rows(path, "SELECT COUNT(*) FROM request_detail WHERE purged_at IS NULL")[0][0] == 0


def test_byte_budget_caps_a_single_batch(body_log_dir, monkeypatch):
    for index in range(3):
        _write_request(f"big-{index}", _ts(2026, 2, 2) + index, body_text="y" * 5000)
    monkeypatch.setattr(body, "BATCH_BYTES", 1)
    commits: list[int] = []
    real_sleep = body.time.sleep
    monkeypatch.setattr(body, "BATCH_PAUSE_SECONDS", 0.001)
    monkeypatch.setattr(body.time, "sleep", lambda s: commits.append(1) or real_sleep(s))
    summary = body.run_body_purge(_ts(2026, 2, 8))
    assert summary["purged_requests"] == 3
    assert len(commits) == 3  # one row per batch once the byte budget is exceeded


def test_byte_length_falls_back_when_sqlite_lacks_octet_length():
    class OldSqlite:
        def execute(self, sql):
            raise sqlite3.OperationalError("no such function: octet_length")

    sql = body._select_sql(OldSqlite(), body._REQUEST_SPEC["select"])
    assert "octet_length" not in sql and "{len:" not in sql
    assert "length(CAST(d.request_body AS BLOB))" in sql
    conn = sqlite3.connect(":memory:")
    try:
        assert "octet_length(d.result_body)" in body._select_sql(conn, body._MCP_SPEC["select"])
    finally:
        conn.close()


def test_mcp_result_bodies_are_purged_by_their_own_time(body_log_dir):
    created = _ts(2026, 2, 2)
    old = log_db.record_mcp_call(call_id="call-old", tool_name="web_search", api_key_name="k", started_at=created)
    log_db.save_mcp_call_detail(old, {"result": "old"}, created_at=created)
    log_db.finish_mcp_call(old, status="success")
    fresh_ts = _ts(2026, 2, 14)
    fresh = log_db.record_mcp_call(call_id="call-fresh", tool_name="web_search", api_key_name="k", started_at=fresh_ts)
    log_db.save_mcp_call_detail(fresh, {"result": "fresh"}, created_at=fresh_ts)
    log_db.finish_mcp_call(fresh, status="success")

    summary = body.run_body_purge(_ts(2026, 2, 8))
    assert summary["purged_mcp"] == 1
    old_detail = log_db.mcp_call_detail("call-old")
    assert old_detail["result_body"] is None and old_detail["purged_at"]
    assert log_db.mcp_call_detail("call-fresh")["result_body"]
    path = body_log_dir / "2026-02.db"
    assert _rows(path, "SELECT COUNT(*) FROM mcp_call_log")[0][0] == 2


def test_detail_views_distinguish_purged_from_never_stored(body_log_dir, monkeypatch):
    _write_request("old", _ts(2026, 2, 2))
    body.run_body_purge(_ts(2026, 2, 8))
    raw = log_db.management_log_detail("old")
    assert raw["log"] is not None and raw["detail"]["purged_at"]
    assert raw["retry_chain"] and raw["billing_attempts"]

    control = LogsControl()
    context = ManagementContext(
        request_id="t", idempotency_key=None,
        actor=ManagementPrincipal.administrator(subject_id="a", auth_method=AuthMethod.MANAGEMENT_KEY),
    )
    detail = control.detail(context, "old")
    assert detail["bodyPurgedAt"] is not None
    assert detail["requestBodyAvailable"] is False
    RequestLogDetailData.model_validate(detail)
    with pytest.raises(ManagementError) as exc:
        control.raw_body(context, "old", kind=LogBodyKind.REQUEST)
    assert exc.value.code is ManagementErrorCode.RESOURCE_NOT_FOUND
    assert "retention" in exc.value.message

    from src.telegram.menus import logs_menu
    note = logs_menu._body_purged_note(raw)
    assert "正文已按保留策略清理" in note
    assert logs_menu._body_purged_note({"detail": {"purged_at": None, "request_body": None}}) == ""


def test_settings_require_plan_for_enabling_or_shortening(body_log_dir):
    _set_body_policy({"mode": "forever", "days": None})
    assert body.update_body_retention_settings(mode="days", days=7)["error"] == "plan_required"
    _set_body_policy({"mode": "days", "days": 7})
    assert body.update_body_retention_settings(mode="days", days=3)["error"] == "plan_required"
    extended = body.update_body_retention_settings(mode="days", days=10)
    assert extended["ok"] is True and config.get()["logBodyRetention"] == {"mode": "days", "days": 10}
    forever = body.update_body_retention_settings(mode="forever")
    assert forever["ok"] is True and config.get()["logBodyRetention"] == {"mode": "forever", "days": None}
    assert body.body_retention_policy({"logBodyRetention": {"mode": "days", "days": 0}})["mode"] == "forever"


def test_plan_then_confirm_saves_policy_and_runs_in_background(body_log_dir):
    _set_body_policy({"mode": "forever", "days": None})
    _write_request("old", _ts(2026, 2, 2))
    _write_request("fresh", time.time())
    plan = body.plan_body_retention(7, now_ts=_ts(2026, 2, 15))
    assert plan["errors"] == []
    assert config.get()["logBodyRetention"] == {"mode": "forever", "days": None}
    item = next(row for row in plan["items"] if row["month"] == "2026-02")
    assert item["expired_requests"] == 1 and item["active"] is False

    result = body.apply_body_retention_plan(plan)
    assert result["ok"] is True and result["config_saved"] is True and result["started"] is True
    assert config.get()["logBodyRetention"] == {"mode": "days", "days": 7}
    status = _wait_idle()
    assert status["purged_requests"] == 1 and status["errors"] == []

    # A stale plan (policy changed since scan) is refused without side effects.
    stale = body.plan_body_retention(3, now_ts=_ts(2026, 2, 15))
    _set_body_policy({"mode": "days", "days": 5})
    refused = body.apply_body_retention_plan(stale)
    assert refused["ok"] is False and "重新扫描" in refused["reason"]
    assert config.get()["logBodyRetention"] == {"mode": "days", "days": 5}


def test_switching_to_forever_stops_the_runner_after_the_current_batch(body_log_dir, monkeypatch):
    for index in range(4):
        _write_request(f"old-{index}", _ts(2026, 2, 2) + index)
    monkeypatch.setattr(body, "BATCH_ROWS", 1)
    monkeypatch.setattr(body, "BATCH_PAUSE_SECONDS", 0.001)
    real_sleep = body.time.sleep
    monkeypatch.setattr(body.time, "sleep", lambda s: (
        _set_body_policy({"mode": "forever", "days": None}), real_sleep(s)))
    summary = body.run_body_purge(_ts(2026, 2, 8))
    assert summary["purged_requests"] == 1
    assert "全部保留" in summary["stopped"]


def test_daily_schedule_starts_background_purge_once_per_day(body_log_dir):
    _write_request("old", _ts(2026, 2, 2))
    started = log_db.maybe_cleanup_retention()
    assert started["skipped"] is True  # row retention stays forever
    status = _wait_idle()
    assert status["purged_requests"] == 1
    assert body.maybe_start_daily()["reason"] == "今日已检查"


def _allow_small_compaction(monkeypatch) -> None:
    monkeypatch.setattr(body, "COMPACT_MIN_MARGIN_BYTES", 0)
    monkeypatch.setattr(body, "COMPACT_MIN_RECLAIM_BYTES", 0)
    monkeypatch.setattr(body, "COMPACT_MIN_RECLAIM_RATIO", 0.0)


def _fill_sealed_month(count: int = 8) -> None:
    big = "z" * 300_000
    for index in range(count):
        _write_request(f"jan-{index}", _ts(2026, 1, 5) + index, body_text=big)


def test_sealed_month_is_compacted_after_full_purge(body_log_dir, monkeypatch):
    _allow_small_compaction(monkeypatch)
    _fill_sealed_month()
    path = body_log_dir / "2026-01.db"
    before = _ap_os.path.getsize(path)
    summary = body.run_body_purge(_ts(2026, 2, 10))
    assert summary["ok"] is True
    [compaction] = summary["compaction"]
    assert compaction["action"] == "compacted", compaction
    assert _ap_os.path.getsize(path) < before / 4
    assert _rows(path, "PRAGMA integrity_check")[0][0] == "ok"
    assert _rows(path, "SELECT COUNT(*) FROM request_log")[0][0] == 8
    assert not _ap_os.path.exists(str(path) + ".compact-tmp")
    assert body.body_retention_status()["last_compaction"][0]["compacted"] is True


def test_compaction_skips_without_space_and_never_touches_current_month(body_log_dir, monkeypatch):
    _allow_small_compaction(monkeypatch)
    _fill_sealed_month()
    path = str(body_log_dir / "2026-01.db")
    body.run_body_purge(_ts(2026, 1, 1))  # nothing expired yet
    monkeypatch.setattr(body.shutil, "disk_usage", lambda _p: SimpleNamespace(free=0))
    summary = body.run_body_purge(_ts(2026, 2, 10))
    assert summary["purged_requests"] == 8
    [compaction] = summary["compaction"]
    assert compaction["action"] == "skipped" and "可用空间不足" in compaction["reason"]

    monkeypatch.undo()
    monkeypatch.setattr(log_db, "_log_dir", str(body_log_dir))
    monkeypatch.setattr(log_db, "_current_db_path", lambda: (path, "2026-01"))
    current = body.compact_month(path, "2026-01")
    assert current["action"] == "skipped" and "当前写入月不压缩" in current["reason"]


def test_concurrent_write_during_vacuum_into_keeps_original_file(body_log_dir, monkeypatch):
    _allow_small_compaction(monkeypatch)
    _fill_sealed_month()
    path = str(body_log_dir / "2026-01.db")
    body.run_body_purge(_ts(2026, 1, 1))
    monkeypatch.setattr(body, "compact_month", lambda *a, **k: {"action": "deferred"})
    body.run_body_purge(_ts(2026, 2, 10))
    monkeypatch.undo()
    monkeypatch.setattr(log_db, "_log_dir", str(body_log_dir))
    _allow_small_compaction(monkeypatch)
    before = _ap_os.path.getsize(path)
    real_fsync = body._fsync_file

    def late_write_then_fsync(tmp):
        conn = sqlite3.connect(path)
        conn.execute("UPDATE request_log SET status='success' WHERE request_id='jan-0'")
        conn.commit()
        conn.close()
        real_fsync(tmp)

    monkeypatch.setattr(body, "_fsync_file", late_write_then_fsync)
    result = body.compact_month(path, "2026-01")
    assert result["action"] == "skipped" and "新写入" in result["reason"]
    assert _ap_os.path.getsize(path) == before
    assert not _ap_os.path.exists(path + ".compact-tmp")
    assert _rows(path, "PRAGMA integrity_check")[0][0] == "ok"


class _FakeBody:
    def __init__(self, cfg):
        self.cfg = cfg
        self.applied = []

    def body_retention_policy(self, cfg=None):
        return body.body_retention_policy(cfg if cfg is not None else self.cfg.value)

    def body_retention_status(self):
        return {"running": True, "month": "2026-10", "purged_requests": 3, "last_compaction": [{
            "month": "2026-09", "action": "skipped", "compacted": False, "reason": "可用空间不足",
            "before_bytes": 10, "after_bytes": 10, "at": 1_700_000_000.0,
        }]}

    def update_body_retention_settings(self, *, mode, days=None, expected_policy=None):
        self.cfg.value["logBodyRetention"] = {"mode": mode, "days": days}
        return {"ok": True}

    def plan_body_retention(self, days):
        return {
            "scope": "bodies", "days": days, "cutoff": 1_699_000_000.0, "reference_ts": 1_700_000_000.0,
            "base_policy": {"mode": "forever", "days": None},
            "items": [{"month": "2023-11", "expired_requests": 4, "expired_mcp": 1, "bundle_bytes": 100}],
            "errors": [], "scanned_months": 1, "scanned_bytes": 100, "scanned_requests": 9,
            "preflight": {"ok": True}, "signature": "sig",
        }

    def apply_body_retention_plan(self, plan, *, activate_policy):
        self.applied.append(plan)
        self.cfg.value["logBodyRetention"] = {"mode": "days", "days": plan["days"]}
        return {"ok": True, "scope": "bodies", "config_saved": True, "days": plan["days"], "started": True}


class _FakeConfig:
    def __init__(self):
        self.value = {"logRetention": {"mode": "forever", "days": None}, "logStoreBodies": True}

    def get(self):
        return self.value

    def update(self, mutate):
        mutate(self.value)
        return self.value


class _FakeRowsDb:
    def retention_policy(self, cfg=None):
        return {"mode": "forever", "days": None}

    def management_logs_count(self):
        return 1

    def retention_cleanup_busy(self):
        return False


def test_management_control_body_settings_plan_and_commit():
    from src.management_control.operations import OperationStore

    cfg = _FakeConfig()
    fake = _FakeBody(cfg)
    control = RetentionControl(log_db=_FakeRowsDb(), config=cfg, body=fake, start_worker=lambda w: w())
    context = ManagementContext(
        request_id="r", idempotency_key=None,
        actor=ManagementPrincipal.administrator(
            subject_id="a", auth_method=AuthMethod.MANAGEMENT_KEY, session_id="s",
        ),
    )
    settings = control.settings(context)
    RetentionSettingsData.model_validate(settings)
    assert settings["bodyMode"] == "forever" and settings["bodyCleanup"]["running"] is True
    assert settings["bodyCleanup"]["compaction"][0]["reason"] == "可用空间不足"

    with pytest.raises(ManagementError) as exc:
        control.update_settings(
            context, mode=None, days=None, log_store_bodies=None, expected_revision=None,
            body_mode=RetentionMode.DAYS, body_days=7,
        )
    assert exc.value.code is ManagementErrorCode.CONFIRMATION_REQUIRED
    assert cfg.value.get("logBodyRetention") is None

    plan = control.create_plan(context, days=7, scope="bodies")
    assert plan["scope"] == "bodies" and plan["affectedRows"] == 5
    operations = OperationStore()
    operation = control.commit_plan(context, plan["id"], expected_revision=plan["revision"], operations=operations)
    done = operations.get(context, operation.id)
    assert done.result["scope"] == "bodies" and done.result["backgroundCleanupStarted"] is True
    assert cfg.value["logBodyRetention"] == {"mode": "days", "days": 7}

    updated = control.update_settings(
        context, mode=None, days=None, log_store_bodies=None, expected_revision=None,
        body_mode=None, body_days=30,
    )
    assert updated["bodyDays"] == 30
    assert control.update_settings(
        context, mode=None, days=None, log_store_bodies=None, expected_revision=None,
        body_mode=RetentionMode.FOREVER,
    )["bodyMode"] == "forever"


def test_management_http_body_retention_extends_existing_operations(tmp_path):
    from src.management_api.routers._observability import controls as resolve_controls
    from src.tests.management_observability_support import build_client

    client, _runtime, _, auth = build_client(tmp_path, inject_controls=False)
    bound = resolve_controls(SimpleNamespace(app=client.app))
    cfg = _FakeConfig()
    bound.retention.config = cfg
    bound.retention.log_db = _FakeRowsDb()
    bound.retention.body = _FakeBody(cfg)
    bound.retention._start_worker = lambda worker: worker()

    current = client.get("/api/management/v1/logs/retention", headers=auth)
    assert current.status_code == 200, current.text
    data = current.json()["data"]
    assert data["bodyMode"] == "forever" and data["bodyCleanup"]["running"] is True

    refused = client.patch(
        "/api/management/v1/logs/retention", json={"bodyMode": "days", "bodyDays": 7}, headers=auth,
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "CONFIRMATION_REQUIRED"

    created = client.post(
        "/api/management/v1/logs/retention/plans", json={"days": 7, "scope": "bodies"}, headers=auth,
    )
    assert created.status_code == 201, created.text
    plan = created.json()["data"]
    assert plan["scope"] == "bodies"
    committed = client.post(
        f"/api/management/v1/logs/retention/plans/{plan['planId']}/commit",
        headers={**auth, "If-Match": plan["revision"]},
    )
    assert committed.status_code == 202, committed.text
    assert cfg.value["logBodyRetention"] == {"mode": "days", "days": 7}
    extended = client.patch(
        "/api/management/v1/logs/retention", json={"bodyDays": 14}, headers=auth,
    )
    assert extended.status_code == 200 and extended.json()["data"]["bodyDays"] == 14
    assert client.post(
        "/api/management/v1/logs/retention/plans", json={"days": 7, "scope": "rows"}, headers=auth,
    ).status_code == 422
