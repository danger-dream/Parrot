"""请求日志按天留存：只读计划、二次确认执行与空间安全契约。"""

from __future__ import annotations

# 必须在任意 src 业务模块导入前安装测试隔离。
import os as _ap_os
import sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src import config, log_db


_BJT = timezone(timedelta(hours=8))


def _ts(year: int, month: int, day: int, hour: int = 0) -> float:
    return datetime(year, month, day, hour, tzinfo=_BJT).timestamp()


@pytest.fixture
def retention_log_dir(tmp_path, monkeypatch):
    local = threading.local()
    registry: dict[str, list[sqlite3.Connection]] = {}
    monkeypatch.setattr(log_db, "_log_dir", str(tmp_path))
    monkeypatch.setattr(log_db, "_local", local)
    monkeypatch.setattr(log_db, "_write_conn_registry", registry)
    monkeypatch.setattr(log_db, "_retired_log_paths", set())
    monkeypatch.setattr(log_db, "_last_retention_cleanup_key", None)
    log_db._request_handles.clear()
    try:
        yield tmp_path
    finally:
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


def _write_complete_request(request_id: str, created_at: float):
    handle = log_db.insert_pending(
        request_id,
        "127.0.0.1",
        "test-key",
        "test-model",
        True,
        1,
        0,
        {"x-test": "yes"},
        {"messages": [{"role": "user", "content": request_id}]},
        created_at=created_at,
    )
    retry = log_db.record_retry_attempt(
        handle, 1, "api:test", "api", "test-model", created_at,
    )
    proxy = log_db.record_proxy_attempt(
        handle, retry, 1, "direct", created_at,
        round_id=f"round-{request_id}", transport="http", request_mode="http_stream",
    )
    log_db.update_proxy_attempt(proxy, ended_at=created_at + 1, outcome="success")
    web = log_db.record_local_web_call(handle, 1, "search", query=request_id, started_at=created_at)
    log_db.finish_local_web_call(web, status="success", ended_at=created_at + 1)
    log_db.finish_success(
        handle, "api:test", "api", "test-model", response_body=f"response:{request_id}",
    )
    return handle


def _table_counts(path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return {
            name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for name in ("request_log", "request_detail", "retry_chain", "proxy_chain", "local_web_log")
        }
    finally:
        conn.close()


def test_plan_and_execution_delete_full_month_and_trim_boundary_month(retention_log_dir):
    # 以 2026-02-15 的“保留 7 天”为例：1 月完整过期，2 月是边界月。
    jan = _write_complete_request("jan-old", _ts(2026, 1, 20))
    feb_old = _write_complete_request("feb-old", _ts(2026, 2, 2))
    feb_fresh = _write_complete_request("feb-fresh", _ts(2026, 2, 12))
    jan_path = jan.db.path
    feb_path = feb_old.db.path

    plan = log_db.plan_retention(7, now_ts=_ts(2026, 2, 15, 12))
    assert plan["errors"] == []
    assert [item["month"] for item in plan["items"]] == ["2026-01", "2026-02"]
    assert [item["action"] for item in plan["items"]] == ["delete_file", "trim_and_vacuum"]
    assert plan["preflight"]["ok"] is True

    result = log_db.apply_retention_plan(plan)
    assert result["ok"] is True, result
    assert result["full_months_deleted"] == 1
    assert result["deleted_requests"] == 2
    assert not _ap_os.path.exists(jan_path)

    counts = _table_counts(feb_path)
    assert counts == {
        "request_log": 1,
        "request_detail": 1,
        "retry_chain": 1,
        "proxy_chain": 1,
        "local_web_log": 1,
    }
    conn = sqlite3.connect(feb_path)
    try:
        assert conn.execute("SELECT request_id FROM request_log").fetchall() == [(feb_fresh.request_id,)]
        assert conn.execute("SELECT request_id FROM request_detail").fetchall() == [(feb_fresh.request_id,)]
    finally:
        conn.close()


def test_second_confirmation_persists_policy_only_when_plan_executes(retention_log_dir):
    config.update(lambda cfg: cfg.__setitem__("logRetention", {"mode": "forever", "days": None}))
    _write_complete_request("old", _ts(2026, 2, 2))
    plan = log_db.plan_retention(7, now_ts=_ts(2026, 2, 15))
    assert config.get()["logRetention"] == {"mode": "forever", "days": None}

    result = log_db.apply_retention_plan(plan, activate_policy=True)
    assert result["ok"] is True, result
    assert result["config_saved"] is True
    assert config.get()["logRetention"] == {"mode": "days", "days": 7}
    config.update(lambda cfg: cfg.__setitem__("logRetention", {"mode": "forever", "days": None}))


def test_extending_existing_day_retention_updates_only_the_day_value(retention_log_dir):
    config.update(lambda cfg: cfg.__setitem__("logRetention", {"mode": "days", "days": 3}))
    try:
        result = log_db.extend_retention_days(5)
        assert result["ok"] is True, result
        assert result["old_days"] == 3
        assert result["days"] == 5
        assert config.get()["logRetention"] == {"mode": "days", "days": 5}
        assert log_db._last_retention_cleanup_key is not None
        assert log_db._last_retention_cleanup_key[0] == 5

        # 缩短或保持原值不允许走无删除确认的延长路径。
        unchanged = log_db.extend_retention_days(5)
        assert unchanged["ok"] is False
        assert "缩短留存期" in unchanged["reason"]
    finally:
        config.update(lambda cfg: cfg.__setitem__("logRetention", {"mode": "forever", "days": None}))


def test_auto_cleanup_applies_saved_policy_once_per_day(retention_log_dir):
    config.update(lambda cfg: cfg.__setitem__("logRetention", {"mode": "days", "days": 7}))
    _write_complete_request("old-auto", _ts(2026, 2, 2))
    now = _ts(2026, 2, 15)

    result = log_db.maybe_cleanup_retention(now_ts=now)
    assert result["ok"] is True, result
    assert result["automatic"] is True
    assert result["deleted_requests"] == 1
    assert _table_counts(retention_log_dir / "2026-02.db")["request_log"] == 0
    assert log_db.maybe_cleanup_retention(now_ts=now)["skipped"] is True
    config.update(lambda cfg: cfg.__setitem__("logRetention", {"mode": "forever", "days": None}))


def test_revalidation_refuses_plan_when_expired_target_changes(retention_log_dir):
    _write_complete_request("old-before-plan", _ts(2026, 2, 2))
    plan = log_db.plan_retention(7, now_ts=_ts(2026, 2, 15))
    # 新出现一条也早于固定 cutoff 的历史记录，不能未经重新预览直接删掉。
    _write_complete_request("old-after-plan", _ts(2026, 2, 3))

    result = log_db.apply_retention_plan(plan)
    assert result["ok"] is False
    assert result["config_saved"] is False
    assert "重新扫描" in result["reason"]
    path = retention_log_dir / "2026-02.db"
    assert _table_counts(path)["request_log"] == 2


def test_preflight_failure_is_fail_closed(retention_log_dir, monkeypatch):
    _write_complete_request("old", _ts(2026, 2, 2))
    plan = log_db.plan_retention(7, now_ts=_ts(2026, 2, 15))
    assert plan["items"]

    monkeypatch.setattr(log_db.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))
    result = log_db.apply_retention_plan(plan)
    assert result["ok"] is False
    assert result["config_saved"] is False
    assert "可用空间不足" in result["reason"]
    assert _table_counts(retention_log_dir / "2026-02.db")["request_log"] == 1


def test_retention_policy_is_fail_closed_for_invalid_config():
    assert log_db.retention_policy({"logRetention": {"mode": "days", "days": 1}}) == {
        "mode": "days", "days": 1,
    }
    for bad in (
        None,
        {},
        {"mode": "days", "days": 0},
        {"mode": "days", "days": "bad"},
        {"mode": "days", "days": 1.5},
        {"mode": "other", "days": 7},
    ):
        assert log_db.retention_policy({"logRetention": bad}) == {"mode": "forever", "days": None}
