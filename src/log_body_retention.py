"""完整正文按期清理（元数据永久保留）与非当前月日志库的安全压缩。

``logBodyRetention`` 只作用于大字段：``request_detail`` 的请求头 / 请求正文 /
响应正文，以及 ``mcp_call_detail`` 的工具返回正文。``request_log``、
``retry_chain``、``upstream_attempt_usage``、``proxy_chain`` 与各类 call_log
永久保留，所有统计、成本与通知照旧可用。

执行方式：

- 清理在独立后台线程中按 ``created_at`` 游标分批推进。每批一个小事务
  （最多 ``BATCH_ROWS`` 行、约 ``BATCH_BYTES`` 正文），只在批内持有日志写锁，
  批与批之间释放写锁并短暂让步，避免请求流水写入长时间等待。
- 仍在进行中的请求 / MCP 调用会被跳过，水位停在它们之前，下次继续处理。
- 已清理的行保留 ``request_detail`` / ``mcp_call_detail`` 记录并写入
  ``purged_at``；详情页据此区分“正文已按保留策略清理”与“未保存正文”。
- 进度水位保存在每个月库自己的 ``parrot_retention_state`` 表中，与批次在同一
  事务提交，进程中途重启后由每日调度从水位继续。
- 当前写入月永远不压缩：释放的页由后续写入复用。非当前月在正文清理完毕后，
  若可用空间 ≥ 存活数据（page_count − freelist_count）+ 余量，用 VACUUM INTO
  生成副本，integrity_check 通过后在日志写锁下原子替换；替换前任何失败都保持
  原文件不变。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Callable

from . import config, log_db


CONFIG_KEY = "logBodyRetention"
_MODE_FOREVER = "forever"
_MODE_DAYS = "days"

# 每批最多处理的行数与正文字节数；任一先到即提交本批并释放写锁。
BATCH_ROWS = 500
BATCH_BYTES = 16 * 1024 * 1024
# 批间让步时间，给请求流水写入留出获取写锁的机会。
BATCH_PAUSE_SECONDS = 0.05
# 自动清理失败后的最短重试间隔（维护循环每 5 分钟检查一次）。
RETRY_INTERVAL_SECONDS = 3600
# 只有可回收空间达到阈值才值得重写整个月库。
COMPACT_MIN_RECLAIM_BYTES = 256 * 1024 * 1024
COMPACT_MIN_RECLAIM_RATIO = 0.25
# VACUUM INTO 需要“存活数据大小”的新文件，再额外预留 10%（至少 512 MiB）。
COMPACT_MIN_MARGIN_BYTES = 512 * 1024 * 1024

_STATE_TABLE = "parrot_retention_state"
_REQUEST_WATERMARK = "request_body_purged_through"
_MCP_WATERMARK = "mcp_body_purged_through"
_COMPACT_SUFFIX = ".compact-tmp"

_settings_lock = threading.Lock()
_compaction_lock = threading.Lock()
_status_lock = threading.Lock()
_stop_event = threading.Event()
_runner_thread: threading.Thread | None = None
_daily_done_key: tuple[int, str] | None = None
_last_attempt_ts = 0.0
_status: dict[str, Any] = {}
_compaction_history: dict[str, dict[str, Any]] = {}


def _idle_status() -> dict[str, Any]:
    return {
        "running": False,
        "automatic": False,
        "days": None,
        "requested_cutoff": None,
        "month": None,
        "started_at": None,
        "finished_at": None,
        "purged_requests": 0,
        "purged_mcp": 0,
        "purged_bytes": 0,
        "skipped_active": 0,
        "errors": [],
        "stopped": "",
    }


_status.update(_idle_status())


# ─── 策略 ──────────────────────────────────────────────────────────

def body_retention_policy(cfg: dict | None = None) -> dict[str, Any]:
    """返回 fail-closed 的正文保留策略；缺失或非法配置一律视为永久保留。"""

    cfg = cfg if isinstance(cfg, dict) else config.get()
    raw = cfg.get(CONFIG_KEY) if isinstance(cfg, dict) else None
    return log_db.retention_policy({"logRetention": raw})


def _is_dangerous(before: dict[str, Any], target: dict[str, Any]) -> bool:
    if target["mode"] != _MODE_DAYS:
        return False
    if before["mode"] != _MODE_DAYS:
        return True
    return int(target["days"]) < int(before["days"])


def _day_key(days: int, ts: float) -> tuple[int, str]:
    return int(days), datetime.fromtimestamp(float(ts), tz=log_db._BJT).strftime("%Y-%m-%d")


def _mark_daily(days: int, ts: float) -> None:
    global _daily_done_key
    with _status_lock:
        _daily_done_key = _day_key(days, ts)


def update_body_retention_settings(
    *,
    mode: str,
    days: Any = None,
    expected_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """保存不会扩大删除范围的正文保留变更（切回永久保留或延长天数）。

    首次启用或缩短天数会扩大删除范围，返回 ``plan_required``，必须经由
    ``plan_body_retention`` / ``apply_body_retention_plan`` 的确认流程。
    """

    global _daily_done_key
    if mode not in {_MODE_FOREVER, _MODE_DAYS}:
        raise log_db.RetentionPlanError("正文保留模式无效")
    if mode == _MODE_FOREVER and days is not None:
        raise log_db.RetentionPlanError("永久保留模式不能设置保留天数")
    target_days = log_db._require_retention_days(days) if mode == _MODE_DAYS else None
    target = {"mode": mode, "days": target_days}
    with _settings_lock:
        before = body_retention_policy()
        if expected_policy is not None and body_retention_policy({CONFIG_KEY: expected_policy}) != before:
            return {"ok": False, "error": "revision_conflict", "reason": "正文保留设置在保存前已被修改"}
        if target == before:
            return {"ok": True, "changed": False, "policy": before}
        if _is_dangerous(before, target):
            return {
                "ok": False,
                "error": "plan_required",
                "reason": "启用或缩短正文保留期必须先扫描并确认清理计划",
            }
        stale = False

        def mutate(candidate: dict[str, Any]) -> None:
            nonlocal stale
            if body_retention_policy(candidate) != before:
                stale = True
                raise log_db.RetentionPlanError("正文保留设置在保存前已被修改")
            candidate[CONFIG_KEY] = dict(target)

        try:
            config.update(mutate)
        except Exception as exc:
            if stale:
                return {"ok": False, "error": "revision_conflict", "reason": str(exc)}
            return {"ok": False, "error": "persist_failed", "reason": f"保存正文保留设置失败：{exc}"}
        if mode == _MODE_FOREVER:
            with _status_lock:
                _daily_done_key = None
        else:
            # 延长保留期不会产生新的删除范围；今日不再触发额外扫描。
            _mark_daily(int(target_days), time.time())
        return {"ok": True, "changed": True, "policy": body_retention_policy()}


# ─── 月库工具 ──────────────────────────────────────────────────────

def _month_bounds(month: str) -> tuple[float, float]:
    year, mon = (int(part) for part in month.split("-", 1))
    start = datetime(year, mon, 1, tzinfo=log_db._BJT)
    end = datetime(year + (mon == 12), 1 if mon == 12 else mon + 1, 1, tzinfo=log_db._BJT)
    return start.timestamp(), end.timestamp()


def _current_path() -> str | None:
    if log_db._log_dir is None:
        return None
    return log_db._current_db_path()[0]


def _path_retired(path: str) -> bool:
    with log_db._write_conn_registry_lock:
        return path in log_db._retired_log_paths


def _open_rw(path: str) -> sqlite3.Connection:
    """打开已存在的月库；``mode=rw`` 保证文件被删除后不会被重新创建。"""

    uri = f"file:{os.path.abspath(path)}?mode=rw"
    conn = sqlite3.connect(uri, uri=True, timeout=10, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_body_schema(conn: sqlite3.Connection) -> set[str]:
    """为旧月库补齐 purged_at 列与水位表；调用方持有日志写锁。"""

    tables = log_db._existing_tables(conn)
    for table in ("request_detail", "mcp_call_detail"):
        if table in tables and "purged_at" not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN purged_at REAL")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_STATE_TABLE} (key TEXT PRIMARY KEY, value REAL NOT NULL)"
    )
    return log_db._existing_tables(conn)


def _read_watermark(conn: sqlite3.Connection, key: str) -> float:
    if _STATE_TABLE not in log_db._existing_tables(conn):
        return 0.0
    row = conn.execute(f"SELECT value FROM {_STATE_TABLE} WHERE key=?", (key,)).fetchone()
    return float(row[0]) if row is not None else 0.0


def _write_watermark(conn: sqlite3.Connection, key: str, value: float) -> None:
    conn.execute(
        f"INSERT INTO {_STATE_TABLE}(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, float(value)),
    )


# ─── 计划与确认 ────────────────────────────────────────────────────

def _count_pending(path: str, cutoff: float) -> dict[str, int]:
    conn = log_db._open_readonly(path)
    try:
        tables = log_db._existing_tables(conn)
        if "request_log" not in tables:
            raise log_db.RetentionPlanError("缺少 request_log 表")
        total = int(conn.execute("SELECT COUNT(*) FROM request_log").fetchone()[0] or 0)
        pending_requests = pending_mcp = 0
        if "request_detail" in tables:
            mark = _read_watermark(conn, _REQUEST_WATERMARK)
            pending_requests = int(conn.execute(
                "SELECT COUNT(*) FROM request_log WHERE created_at >= ? AND created_at < ?",
                (mark, float(cutoff)),
            ).fetchone()[0] or 0)
        if "mcp_call_detail" in tables:
            mark = _read_watermark(conn, _MCP_WATERMARK)
            pending_mcp = int(conn.execute(
                "SELECT COUNT(*) FROM mcp_call_detail WHERE created_at >= ? AND created_at < ?",
                (mark, float(cutoff)),
            ).fetchone()[0] or 0)
        return {"total_requests": total, "pending_requests": pending_requests, "pending_mcp": pending_mcp}
    finally:
        conn.close()


def plan_body_retention(days: int, now_ts: float | None = None) -> dict[str, Any]:
    """只读扫描正文清理范围，供第二次确认页展示；不写入任何文件。"""

    now = time.time() if now_ts is None else float(now_ts)
    days = log_db._require_retention_days(days)
    cutoff = now - days * 86400
    plan: dict[str, Any] = {
        "scope": "bodies",
        "days": days,
        "cutoff": cutoff,
        "reference_ts": now,
        "created_at": time.time(),
        "base_policy": body_retention_policy(),
        "items": [],
        "errors": [],
        "scanned_months": 0,
        "scanned_bytes": 0,
        "scanned_requests": 0,
        "preflight": {"ok": True, "reason": ""},
    }
    if log_db._log_dir is None:
        plan["errors"].append("日志目录尚未初始化")
    current = _current_path()
    for month, path in log_db._monthly_log_files():
        bundle = log_db._log_bundle_bytes(path)
        plan["scanned_months"] += 1
        plan["scanned_bytes"] += bundle
        start, end = _month_bounds(month)
        try:
            counts = _count_pending(path, cutoff)
        except Exception as exc:
            plan["errors"].append(f"{month}.db 无法安全扫描：{exc}")
            continue
        plan["scanned_requests"] += counts["total_requests"]
        if start >= cutoff or counts["pending_requests"] + counts["pending_mcp"] <= 0:
            continue
        active = path == current
        plan["items"].append({
            "month": month,
            "path": path,
            "action": "purge_bodies",
            "cutoff": cutoff,
            "db_bytes": int(os.path.getsize(path)),
            "bundle_bytes": bundle,
            "total_requests": counts["total_requests"],
            "expired_requests": counts["pending_requests"],
            "expired_mcp": counts["pending_mcp"],
            "active": active,
            "compact_after": (not active) and end <= cutoff,
        })
    if plan["errors"]:
        plan["preflight"] = {"ok": False, "reason": "扫描存在错误，不能执行清理"}
    raw = json.dumps({"days": days, "cutoff": f"{cutoff:.6f}", "scope": "bodies"}, sort_keys=True)
    plan["signature"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return plan


def apply_body_retention_plan(plan: dict[str, Any], *, activate_policy: bool = True) -> dict[str, Any]:
    """确认后保存正文保留策略并启动后台清理；清理本身不在调用线程执行。"""

    if not isinstance(plan, dict) or plan.get("scope") != "bodies":
        return {"ok": False, "config_saved": False, "reason": "正文清理计划格式无效"}
    try:
        days = log_db._require_retention_days(plan.get("days"))
        cutoff = float(plan["cutoff"])
        reference_ts = float(plan["reference_ts"])
    except (KeyError, TypeError, ValueError, log_db.RetentionPlanError):
        return {"ok": False, "config_saved": False, "reason": "正文清理计划缺少时间边界"}
    if plan.get("errors"):
        return {"ok": False, "config_saved": False, "reason": "；".join(str(x) for x in plan["errors"])}
    base = plan.get("base_policy")
    if not isinstance(base, dict):
        return {"ok": False, "config_saved": False, "reason": "正文清理计划缺少原始策略快照"}
    config_saved = False
    with _settings_lock:
        if body_retention_policy() != body_retention_policy({CONFIG_KEY: base}):
            return {
                "ok": False,
                "config_saved": False,
                "reason": "正文保留策略在确认期间已被修改，请重新扫描后再确认",
            }
        if activate_policy:
            try:
                config.update(lambda cfg: cfg.__setitem__(CONFIG_KEY, {"mode": _MODE_DAYS, "days": days}))
                config_saved = True
            except Exception as exc:
                return {"ok": False, "config_saved": False, "reason": f"保存正文保留策略失败：{exc}"}
    started = start_body_purge(days=days, cutoff=cutoff, reference_ts=reference_ts)
    return {
        "ok": True,
        "scope": "bodies",
        "config_saved": config_saved,
        "days": days,
        "started": bool(started.get("started")),
        "reason": str(started.get("reason") or ""),
    }


# ─── 后台清理 ──────────────────────────────────────────────────────

def body_cleanup_busy() -> bool:
    with _status_lock:
        return bool(_status.get("running"))


def body_retention_status() -> dict[str, Any]:
    with _status_lock:
        value = copy.deepcopy(_status)
        value["last_compaction"] = [
            copy.deepcopy(_compaction_history[month]) for month in sorted(_compaction_history)
        ]
    return value


def _update_status(**changes: Any) -> None:
    with _status_lock:
        for key, value in changes.items():
            if key in {"purged_requests", "purged_mcp", "purged_bytes", "skipped_active"}:
                _status[key] = int(_status.get(key) or 0) + int(value or 0)
            else:
                _status[key] = value


def _record_compaction(result: dict[str, Any]) -> None:
    with _status_lock:
        _compaction_history[str(result.get("month") or "?")] = copy.deepcopy(result)
        while len(_compaction_history) > 24:
            _compaction_history.pop(sorted(_compaction_history)[0], None)


def start_body_purge(
    *,
    days: int,
    cutoff: float,
    reference_ts: float | None = None,
    automatic: bool = False,
) -> dict[str, Any]:
    """启动（或追加到）唯一的后台正文清理线程。"""

    global _runner_thread
    reference = time.time() if reference_ts is None else float(reference_ts)
    with _status_lock:
        alive = _runner_thread is not None and _runner_thread.is_alive()
        if alive:
            # 运行中的线程每批都会重新读取截止时间，直接推进即可。
            _status["requested_cutoff"] = max(float(_status.get("requested_cutoff") or 0), float(cutoff))
            _status["days"] = int(days)
            return {"started": False, "running": True, "reason": "后台正文清理已在运行，将按最新策略继续"}
        _status.clear()
        _status.update(_idle_status())
        _status.update({
            "running": True,
            "automatic": bool(automatic),
            "days": int(days),
            "requested_cutoff": float(cutoff),
            "started_at": time.time(),
        })
        _stop_event.clear()
        thread = threading.Thread(
            target=_runner_main,
            args=(_day_key(days, reference),),
            name="parrot-body-retention",
            daemon=True,
        )
        _runner_thread = thread
    thread.start()
    return {"started": True, "running": True, "reason": ""}


def stop_body_purge(timeout: float | None = None) -> None:
    """请求后台清理在当前批次后停止（测试与关闭流程使用）。"""

    _stop_event.set()
    thread = _runner_thread
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout)


def _requested_cutoff() -> float:
    with _status_lock:
        return float(_status.get("requested_cutoff") or 0.0)


def _runner_main(day_key: tuple[int, str]) -> None:
    global _daily_done_key
    summary: dict[str, Any] = {"ok": False, "errors": [], "stopped": ""}
    try:
        while True:
            pass_cutoff = _requested_cutoff()
            summary = run_body_purge(_requested_cutoff)
            if (not summary.get("ok") or summary.get("stopped")
                    or _requested_cutoff() <= pass_cutoff):
                break
    except Exception as exc:  # 线程边界：记录后由下次调度重试
        summary = {"ok": False, "errors": [f"后台正文清理失败：{exc}"], "stopped": ""}
    finally:
        with _status_lock:
            _status["running"] = False
            _status["month"] = None
            _status["finished_at"] = time.time()
            _status["errors"] = list(summary.get("errors") or [])
            _status["stopped"] = str(summary.get("stopped") or "")
            if summary.get("ok") and not summary.get("stopped"):
                _daily_done_key = day_key
            purged = int(_status.get("purged_requests") or 0) + int(_status.get("purged_mcp") or 0)
            freed = int(_status.get("purged_bytes") or 0)
        print(f"[log_db] body retention purged {purged} bodies (~{freed} bytes)"
              + (f"; errors: {summary.get('errors')}" if summary.get("errors") else ""))


def _effective_cutoff(requested: float) -> float | None:
    policy = body_retention_policy()
    if policy["mode"] != _MODE_DAYS:
        return None
    return min(float(requested), time.time() - int(policy["days"]) * 86400)


def run_body_purge(cutoff: float | Callable[[], float]) -> dict[str, Any]:
    """同步执行一轮正文清理（后台线程主体；测试也直接调用）。

    ``cutoff`` 可以是固定时间戳或返回时间戳的函数；每批都会与当前已保存
    的保留策略取较早者，策略切回永久保留时在当前批后停止。
    """

    requested = cutoff if callable(cutoff) else (lambda value=float(cutoff): value)
    summary: dict[str, Any] = {
        "ok": True, "purged_requests": 0, "purged_mcp": 0, "purged_bytes": 0,
        "skipped_active": 0, "months": [], "compaction": [], "errors": [], "stopped": "",
    }
    if log_db._log_dir is None:
        summary.update(ok=False, errors=["日志目录尚未初始化"])
        return summary

    def cutoff_now() -> float | None:
        return _effective_cutoff(requested())

    current = _current_path()
    for month, path in log_db._monthly_log_files():
        if _stop_event.is_set():
            summary["stopped"] = "已请求停止"
            break
        effective = cutoff_now()
        if effective is None:
            summary["stopped"] = "正文保留策略已切换为全部保留"
            break
        start, end = _month_bounds(month)
        if start >= effective:
            continue
        _update_status(month=month)
        try:
            result = _purge_month(month, path, cutoff_now)
        except Exception as exc:
            summary["errors"].append(f"{month}.db 正文清理失败：{exc}")
            continue
        summary["months"].append(result)
        for key in ("purged_requests", "purged_mcp", "purged_bytes", "skipped_active"):
            summary[key] += int(result.get(key) or 0)
        if result.get("stopped"):
            summary["stopped"] = str(result["stopped"])
            break
        effective = cutoff_now()
        if (result.get("complete") and effective is not None and path != current
                and end <= effective):
            summary["compaction"].append(compact_month(path, month))
    summary["ok"] = not summary["errors"]
    return summary


def _active_request_keys(conn: sqlite3.Connection, path: str) -> set[str]:
    return {
        rid for rid, handle in list(log_db._request_handles.items())
        if handle.db.path == path
    }


def _active_mcp_keys(conn: sqlite3.Connection, path: str) -> set[str]:
    ids = log_db._active_call_ids(path, "mcp_call_log")
    if not ids or "mcp_call_log" not in log_db._existing_tables(conn):
        return set()
    marks = ",".join("?" for _ in ids)
    return {
        str(row[0]) for row in conn.execute(
            f"SELECT call_id FROM mcp_call_log WHERE id IN ({marks})", tuple(ids),
        ).fetchall()
    }


_REQUEST_SPEC = {
    "table": "request_detail",
    "watermark": _REQUEST_WATERMARK,
    "counter": "purged_requests",
    # CROSS JOIN 固定以 request_log 的 created_at 索引驱动扫描。
    "select": (
        "SELECT l.request_id AS key, l.created_at AS ts, l.id AS seq, "
        "COALESCE({len:d.request_headers},0)+COALESCE({len:d.request_body},0)"
        "+COALESCE({len:d.response_body},0) AS bytes "
        "FROM request_log l CROSS JOIN request_detail d ON d.request_id=l.request_id "
        "WHERE l.created_at >= ? AND l.created_at < ? AND (l.created_at > ? OR l.id > ?) "
        "AND d.purged_at IS NULL AND (d.request_headers IS NOT NULL "
        "OR d.request_body IS NOT NULL OR d.response_body IS NOT NULL) "
        "ORDER BY l.created_at, l.id LIMIT ?"
    ),
    "update": (
        "UPDATE request_detail SET request_headers=NULL, request_body=NULL, "
        "response_body=NULL, purged_at=? WHERE request_id=? AND purged_at IS NULL"
    ),
    "active": _active_request_keys,
}
_MCP_SPEC = {
    "table": "mcp_call_detail",
    "watermark": _MCP_WATERMARK,
    "counter": "purged_mcp",
    "select": (
        "SELECT d.call_id AS key, d.created_at AS ts, d.rowid AS seq, "
        "COALESCE({len:d.result_body},0) AS bytes FROM mcp_call_detail d "
        "WHERE d.created_at >= ? AND d.created_at < ? AND (d.created_at > ? OR d.rowid > ?) "
        "AND d.purged_at IS NULL AND d.result_body IS NOT NULL "
        "ORDER BY d.created_at, d.rowid LIMIT ?"
    ),
    "update": (
        "UPDATE mcp_call_detail SET result_body=NULL, purged_at=? "
        "WHERE call_id=? AND purged_at IS NULL"
    ),
    "active": _active_mcp_keys,
}


def _select_sql(conn: sqlite3.Connection, template: str) -> str:
    """Fill byte-length expressions; octet_length() needs SQLite >= 3.43.

    octet_length avoids decoding the text; older SQLite builds (e.g. the
    Debian-based python:3.11-slim image) fall back to length(CAST(x AS BLOB)).
    """

    try:
        conn.execute("SELECT octet_length('x')").fetchone()
        wrap = "octet_length({})"
    except sqlite3.OperationalError:
        wrap = "length(CAST({} AS BLOB))"
    out = template
    while "{len:" in out:
        start = out.index("{len:")
        end = out.index("}", start)
        out = out[:start] + wrap.format(out[start + 5:end]) + out[end + 1:]
    return out


def _purge_month(month: str, path: str, cutoff_now: Callable[[], float | None]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "month": month, "purged_requests": 0, "purged_mcp": 0, "purged_bytes": 0,
        "skipped_active": 0, "complete": False, "stopped": "",
    }
    if not os.path.exists(path) or _path_retired(path):
        result["complete"] = True
        return result
    conn = _open_rw(path)
    try:
        with log_db._write_lock:
            if _path_retired(path):
                result["complete"] = True
                return result
            tables = _ensure_body_schema(conn)
        complete = True
        for spec in (_REQUEST_SPEC, _MCP_SPEC):
            if spec["table"] not in tables:
                continue
            if spec is _REQUEST_SPEC and "request_log" not in tables:
                continue
            done = _purge_table(conn, path, spec, cutoff_now, result)
            if result["stopped"]:
                return result
            complete = complete and done
        result["complete"] = complete
        return result
    finally:
        conn.close()


def _purge_table(
    conn: sqlite3.Connection,
    path: str,
    spec: dict[str, Any],
    cutoff_now: Callable[[], float | None],
    result: dict[str, Any],
) -> bool:
    """按游标分批清理一张正文表；返回该表在截止时间前是否已全部处理。"""

    mark = _read_watermark(conn, spec["watermark"])
    select_sql = _select_sql(conn, spec["select"])
    cursor_ts, cursor_seq = mark, -1
    hold: float | None = None
    while True:
        if _stop_event.is_set():
            result["stopped"] = "已请求停止"
            return False
        cutoff = cutoff_now()
        if cutoff is None:
            result["stopped"] = "正文保留策略已切换为全部保留"
            return False
        rows = conn.execute(
            select_sql, (cursor_ts, cutoff, cursor_ts, cursor_seq, int(BATCH_ROWS)),
        ).fetchall()
        if not rows:
            final = cutoff if hold is None else min(hold, cutoff)
            if final > mark:
                with log_db._write_lock:
                    if _path_retired(path):
                        return True
                    _write_watermark(conn, spec["watermark"], final)
            return hold is None
        batch: list[sqlite3.Row] = []
        batch_bytes = 0
        for row in rows:
            batch.append(row)
            batch_bytes += int(row["bytes"] or 0)
            if batch_bytes >= BATCH_BYTES:
                break
        purged = purged_bytes = skipped = 0
        with log_db._write_lock:
            if _path_retired(path) or not os.path.exists(path):
                result["stopped"] = "该月日志库已被整库删除"
                return False
            active = spec["active"](conn, path)
            now = time.time()
            conn.execute("BEGIN IMMEDIATE")
            try:
                for row in batch:
                    if str(row["key"]) in active:
                        skipped += 1
                        ts = float(row["ts"])
                        hold = ts if hold is None else min(hold, ts)
                        continue
                    cur = conn.execute(spec["update"], (now, row["key"]))
                    if cur.rowcount:
                        purged += 1
                        purged_bytes += int(row["bytes"] or 0)
                last_ts = float(batch[-1]["ts"])
                new_mark = last_ts if hold is None else min(hold, last_ts)
                if new_mark > mark:
                    _write_watermark(conn, spec["watermark"], new_mark)
                    mark = new_mark
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        cursor_ts, cursor_seq = float(batch[-1]["ts"]), int(batch[-1]["seq"])
        result[spec["counter"]] += purged
        result["purged_bytes"] += purged_bytes
        result["skipped_active"] += skipped
        _update_status(**{spec["counter"]: purged, "purged_bytes": purged_bytes, "skipped_active": skipped})
        if BATCH_PAUSE_SECONDS > 0:
            time.sleep(BATCH_PAUSE_SECONDS)


# ─── 压缩 ──────────────────────────────────────────────────────────

def _remove_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _fsync_file(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def compact_month(
    path: str,
    month: str,
    *,
    reference_month: str | None = None,
) -> dict[str, Any]:
    """用 VACUUM INTO 压缩一个非当前写入月；空间不足或有并发写入时安全跳过。

    返回 ``action``：``compacted`` / ``skipped`` / ``not_needed`` / ``failed``；
    除 ``compacted`` 外原文件保持不变，``reason`` 如实说明原因。
    """

    before = log_db._log_bundle_bytes(path)
    result: dict[str, Any] = {
        "month": month, "action": "skipped", "compacted": False, "reason": "",
        "before_bytes": before, "after_bytes": before, "live_bytes": 0,
        "required_bytes": 0, "available_bytes": 0, "at": time.time(),
    }
    if path == _current_path() or (reference_month is not None and month == reference_month):
        result["reason"] = "当前写入月不压缩：释放的空间会被后续写入复用"
        return result
    if not os.path.exists(path):
        result["reason"] = "日志库已不存在"
        return result
    if not _compaction_lock.acquire(blocking=False):
        result["reason"] = "另一个压缩任务正在进行，稍后由维护任务重试"
        return result
    tmp = path + _COMPACT_SUFFIX
    src: sqlite3.Connection | None = None
    old_fd: int | None = None
    try:
        src = _open_rw(path)
        page_size = int(src.execute("PRAGMA page_size").fetchone()[0] or 0)
        page_count = int(src.execute("PRAGMA page_count").fetchone()[0] or 0)
        freelist = int(src.execute("PRAGMA freelist_count").fetchone()[0] or 0)
        live = max(0, page_count - freelist) * page_size
        file_bytes = int(os.path.getsize(path))
        result["live_bytes"] = live
        reclaim = max(0, file_bytes - live)
        if reclaim < max(COMPACT_MIN_RECLAIM_BYTES, int(file_bytes * COMPACT_MIN_RECLAIM_RATIO)):
            result["action"] = "not_needed"
            result["reason"] = "可回收空间较少，无需压缩"
            return result
        required = live + max(COMPACT_MIN_MARGIN_BYTES, live // 10)
        free = int(shutil.disk_usage(os.path.dirname(os.path.abspath(path))).free)
        result["required_bytes"] = required
        result["available_bytes"] = free
        if free < required:
            result["reason"] = (
                f"可用空间不足：压缩需要约 {required} 字节（存活数据 {live} 字节 + 余量），"
                f"当前可用 {free} 字节；已删除数据占用的页仍可被后续写入复用"
            )
            return result
        for suffix in ("", "-journal", "-wal", "-shm"):
            _remove_quietly(tmp + suffix)
        version = int(src.execute("PRAGMA data_version").fetchone()[0])
        src.execute("VACUUM INTO ?", (tmp,))
        check = sqlite3.connect(tmp, isolation_level=None)
        try:
            verdict = [str(row[0]) for row in check.execute("PRAGMA integrity_check").fetchall()]
            if verdict != ["ok"]:
                raise log_db.RetentionPlanError(f"压缩副本完整性检查失败：{'; '.join(verdict[:3])}")
            check.execute("PRAGMA journal_mode=WAL")
        finally:
            check.close()
        _fsync_file(tmp)
        with log_db._write_lock:
            if _path_retired(path) or not os.path.exists(path):
                result["reason"] = "该月日志库已被留存清理删除，放弃替换"
                return result
            if int(src.execute("PRAGMA data_version").fetchone()[0]) != version:
                result["reason"] = "压缩期间该月有新写入，已放弃替换（原文件保持不变），下次维护时重试"
                return result
            if log_db._has_active_handle_for_path(path):
                result["reason"] = "该月仍有进行中的请求，已放弃替换（原文件保持不变），下次维护时重试"
                return result
            log_db._close_cached_write_connections(path)
            src.execute("PRAGMA busy_timeout=1000")
            busy = src.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if busy is not None and int(busy[0] or 0) != 0:
                result["reason"] = "原库仍有读取占用 WAL，已放弃替换（原文件保持不变），下次维护时重试"
                return result
            src.close()
            src = None
            # 保留旧 inode 的句柄，让内核在释放写锁之后（close 时）再回收旧文件
            # 的数据块；否则替换百 GB 级文件时 rename 会在锁内同步释放空间。
            old_fd = os.open(path, os.O_RDONLY)
            os.replace(tmp, path)
            for suffix in ("-wal", "-shm"):
                _remove_quietly(path + suffix)
        _fsync_dir(os.path.dirname(os.path.abspath(path)))
        result["action"] = "compacted"
        result["compacted"] = True
        result["reason"] = ""
        return result
    except Exception as exc:
        result["action"] = "failed"
        result["reason"] = f"压缩失败，原文件保持不变：{exc}"
        return result
    finally:
        if src is not None:
            try:
                src.close()
            except Exception:
                pass
        if old_fd is not None:
            os.close(old_fd)
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                _remove_quietly(tmp + suffix)
            except OSError:
                pass
        _compaction_lock.release()
        result["after_bytes"] = log_db._log_bundle_bytes(path)
        result["at"] = time.time()
        _record_compaction(result)


# ─── 每日调度 ──────────────────────────────────────────────────────

def maybe_start_daily(now_ts: float | None = None) -> dict[str, Any]:
    """由 ``log_db.maybe_cleanup_retention`` 调用：按已保存策略启动后台清理。"""

    global _last_attempt_ts
    now = time.time() if now_ts is None else float(now_ts)
    policy = body_retention_policy()
    if policy["mode"] != _MODE_DAYS:
        return {"ok": True, "skipped": True, "reason": "正文永久保留"}
    days = int(policy["days"])
    key = _day_key(days, now)
    with _status_lock:
        if _daily_done_key == key:
            return {"ok": True, "skipped": True, "reason": "今日已检查"}
        if _status.get("running"):
            return {"ok": True, "skipped": True, "reason": "后台正文清理正在运行"}
        if _last_attempt_ts and now - _last_attempt_ts < RETRY_INTERVAL_SECONDS:
            return {"ok": True, "skipped": True, "reason": "等待重试间隔"}
        _last_attempt_ts = now
    started = start_body_purge(days=days, cutoff=now - days * 86400, reference_ts=now, automatic=True)
    return {"ok": True, "skipped": False, "started": bool(started.get("started"))}


def _reset_for_tests() -> None:
    global _daily_done_key, _last_attempt_ts, _runner_thread
    stop_body_purge(timeout=10)
    with _status_lock:
        _status.clear()
        _status.update(_idle_status())
        _compaction_history.clear()
        _daily_done_key = None
        _last_attempt_ts = 0.0
        _runner_thread = None
    _stop_event.clear()
