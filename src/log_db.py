"""按月分库的业务日志。

文件名 logs/YYYY-MM.db，按北京时间判断月份。
三张表：
  - request_log      请求摘要（供统计与列表）
  - request_detail   大字段（headers / body / response_body）
  - retry_chain      重试链（每次尝试一条记录）

写操作由 `_write_lock` 序列化；跨月自动切换连接。
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config

_BJT = timezone(timedelta(hours=8))
_local = threading.local()
# 可重入：_get_conn 在新线程首次创建连接时自身需持锁做 CREATE TABLE，
# 而上层写函数先取锁再调 _get_conn；非重入锁会死锁。
_write_lock = threading.RLock()
_initialized = False
_log_dir: str | None = None


def _resolve_log_dir() -> str:
    cfg = config.get()
    rel = cfg.get("logDir", "logs")
    if os.path.isabs(rel):
        return rel
    # Relative paths anchor to DATA_DIR (container: /app/data; source install: BASE_DIR).
    return os.path.join(config.DATA_DIR, rel)


def _schema_sql() -> str:
    return """
    CREATE TABLE IF NOT EXISTS request_log (
      id                    INTEGER PRIMARY KEY AUTOINCREMENT,
      request_id            TEXT UNIQUE NOT NULL,
      created_at            REAL NOT NULL,
      finished_at           REAL,
      client_ip             TEXT,
      api_key_name          TEXT,
      requested_model       TEXT,
      final_channel_key     TEXT,
      final_channel_type    TEXT,
      final_model           TEXT,
      status                TEXT DEFAULT 'pending',
      http_status           INTEGER,
      error_message         TEXT,
      is_stream             INTEGER DEFAULT 1,
      msg_count             INTEGER DEFAULT 0,
      tool_count            INTEGER DEFAULT 0,
      input_tokens          INTEGER DEFAULT 0,
      output_tokens         INTEGER DEFAULT 0,
      cache_creation_tokens INTEGER DEFAULT 0,
      cache_read_tokens     INTEGER DEFAULT 0,
      connect_time_ms       INTEGER,
      first_token_time_ms   INTEGER,
      total_time_ms         INTEGER,
      retry_count           INTEGER DEFAULT 0,
      affinity_hit          INTEGER DEFAULT 0,
      fingerprint           TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_log_created ON request_log(created_at);
    CREATE INDEX IF NOT EXISTS idx_log_status ON request_log(status);
    CREATE INDEX IF NOT EXISTS idx_log_apikey ON request_log(api_key_name);
    CREATE INDEX IF NOT EXISTS idx_log_channel ON request_log(final_channel_key);
    CREATE INDEX IF NOT EXISTS idx_log_model ON request_log(requested_model);

    CREATE TABLE IF NOT EXISTS request_detail (
      request_id       TEXT PRIMARY KEY,
      request_headers  TEXT,
      request_body     TEXT,
      response_body    TEXT
    );

    CREATE TABLE IF NOT EXISTS retry_chain (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      request_id      TEXT NOT NULL,
      attempt_order   INTEGER NOT NULL,
      channel_key     TEXT NOT NULL,
      channel_type    TEXT NOT NULL,
      model           TEXT NOT NULL,
      started_at      REAL NOT NULL,
      connect_ms      INTEGER,
      first_byte_ms   INTEGER,
      ended_at        REAL,
      outcome         TEXT,
      error_detail    TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_retry_req ON retry_chain(request_id);
    """


def init() -> None:
    global _initialized, _log_dir
    if _initialized:
        return
    _log_dir = _resolve_log_dir()
    os.makedirs(_log_dir, exist_ok=True)
    # 预热当月连接
    _get_conn()
    _initialized = True
    path, _ = _current_db_path()
    print(f"[log_db] Using {path}")


def _current_db_path() -> tuple[str, str]:
    assert _log_dir is not None
    month = datetime.now(_BJT).strftime("%Y-%m")
    return os.path.join(_log_dir, f"{month}.db"), month


def _get_conn() -> sqlite3.Connection:
    """按月切换 thread-local 连接；跨月时关闭旧连接建立新连接。"""
    if _log_dir is None:
        raise RuntimeError("log_db.init() not called")
    path, month = _current_db_path()
    need_new = (
        getattr(_local, "conn", None) is None
        or getattr(_local, "month", None) != month
    )
    if need_new:
        old = getattr(_local, "conn", None)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        with _write_lock:
            conn.executescript(_schema_sql())
            conn.commit()
        _local.conn = conn
        _local.month = month
    return _local.conn


def _get_conn_for_month(month: str) -> sqlite3.Connection | None:
    """只读方式打开指定月份的 DB。若文件不存在返回 None。"""
    if _log_dir is None:
        return None
    path = os.path.join(_log_dir, f"{month}.db")
    if not os.path.exists(path):
        return None
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def checkpoint() -> None:
    try:
        _get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass


# ─── 写入 ──────────────────────────────────────────────────────────

def insert_pending(
    request_id: str,
    client_ip: str,
    api_key_name: str | None,
    requested_model: str | None,
    is_stream: bool,
    msg_count: int,
    tool_count: int,
    request_headers: dict | None,
    request_body: dict | None,
    fingerprint: str | None = None,
) -> None:
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            """INSERT INTO request_log
               (request_id, created_at, client_ip, api_key_name, requested_model,
                status, is_stream, msg_count, tool_count, fingerprint)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                request_id, time.time(), client_ip, api_key_name, requested_model,
                "pending", 1 if is_stream else 0, msg_count, tool_count,
                # log 表只存 16 字符（节省空间）；它是 affinity 表 32 字符指纹的前缀，
                # 排查时可用 `log.fingerprint || '%'` 做前缀匹配反查 cache_affinities。
                fingerprint[:16] if fingerprint else None,
            ),
        )
        conn.execute(
            """INSERT INTO request_detail (request_id, request_headers, request_body)
               VALUES (?,?,?)""",
            (
                request_id,
                json.dumps(request_headers, ensure_ascii=False) if request_headers else None,
                json.dumps(request_body, ensure_ascii=False) if request_body else None,
            ),
        )
        conn.commit()


def update_pending(request_id: str, **fields: Any) -> None:
    """在 pending 阶段追加一些字段（如 fingerprint / affinity_hit）。"""
    if not fields:
        return
    allowed = {
        "fingerprint", "affinity_hit", "requested_model",
        "msg_count", "tool_count",
    }
    cols, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        cols.append(f"{k}=?")
        if k == "fingerprint" and v is not None:
            v = v[:16]
        vals.append(v)
    if not cols:
        return
    vals.append(request_id)
    with _write_lock:
        _get_conn().execute(
            f"UPDATE request_log SET {', '.join(cols)} WHERE request_id=?",
            vals,
        )
        _get_conn().commit()


def record_retry_attempt(
    request_id: str, attempt_order: int,
    channel_key: str, channel_type: str, model: str,
    started_at: float,
) -> int:
    """插入一次尝试记录，返回该条的 id，后续用 update_retry_attempt 补齐。"""
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute(
            """INSERT INTO retry_chain
               (request_id, attempt_order, channel_key, channel_type, model, started_at)
               VALUES (?,?,?,?,?,?)""",
            (request_id, attempt_order, channel_key, channel_type, model, started_at),
        )
        conn.commit()
        return int(cur.lastrowid)


def update_retry_attempt(
    attempt_id: int,
    connect_ms: int | None = None,
    first_byte_ms: int | None = None,
    ended_at: float | None = None,
    outcome: str | None = None,
    error_detail: str | None = None,
) -> None:
    fields, vals = [], []
    if connect_ms is not None:
        fields.append("connect_ms=?"); vals.append(connect_ms)
    if first_byte_ms is not None:
        fields.append("first_byte_ms=?"); vals.append(first_byte_ms)
    if ended_at is not None:
        fields.append("ended_at=?"); vals.append(ended_at)
    if outcome is not None:
        fields.append("outcome=?"); vals.append(outcome)
    if error_detail is not None:
        fields.append("error_detail=?"); vals.append(error_detail)
    if not fields:
        return
    vals.append(attempt_id)
    with _write_lock:
        _get_conn().execute(
            f"UPDATE retry_chain SET {', '.join(fields)} WHERE id=?",
            vals,
        )
        _get_conn().commit()


def finish_success(
    request_id: str,
    final_channel_key: str,
    final_channel_type: str,
    final_model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    connect_ms: int | None = None,
    first_token_ms: int | None = None,
    total_ms: int | None = None,
    retry_count: int = 0,
    affinity_hit: int = 0,
    response_body: str | None = None,
    http_status: int = 200,
) -> None:
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            """UPDATE request_log SET
                 status='success', finished_at=?, http_status=?,
                 final_channel_key=?, final_channel_type=?, final_model=?,
                 input_tokens=?, output_tokens=?,
                 cache_creation_tokens=?, cache_read_tokens=?,
                 connect_time_ms=?, first_token_time_ms=?, total_time_ms=?,
                 retry_count=?, affinity_hit=?
               WHERE request_id=?""",
            (
                time.time(), http_status,
                final_channel_key, final_channel_type, final_model,
                input_tokens, output_tokens,
                cache_creation_tokens, cache_read_tokens,
                connect_ms, first_token_ms, total_ms,
                retry_count, affinity_hit,
                request_id,
            ),
        )
        if response_body is not None:
            conn.execute(
                "UPDATE request_detail SET response_body=? WHERE request_id=?",
                (response_body, request_id),
            )
        conn.commit()


def finish_error(
    request_id: str,
    error_message: str,
    retry_count: int = 0,
    final_channel_key: str | None = None,
    final_channel_type: str | None = None,
    final_model: str | None = None,
    connect_ms: int | None = None,
    first_token_ms: int | None = None,
    total_ms: int | None = None,
    http_status: int | None = None,
    response_body: str | None = None,
    affinity_hit: int = 0,
) -> None:
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            """UPDATE request_log SET
                 status='error', finished_at=?, error_message=?, http_status=?,
                 final_channel_key=?, final_channel_type=?, final_model=?,
                 connect_time_ms=?, first_token_time_ms=?, total_time_ms=?,
                 retry_count=?, affinity_hit=?
               WHERE request_id=?""",
            (
                time.time(), error_message, http_status,
                final_channel_key, final_channel_type, final_model,
                connect_ms, first_token_ms, total_ms,
                retry_count, affinity_hit,
                request_id,
            ),
        )
        if response_body is not None:
            conn.execute(
                "UPDATE request_detail SET response_body=? WHERE request_id=?",
                (response_body, request_id),
            )
        conn.commit()


def stats_lifetime() -> dict:
    """跨所有月份 db 的累计统计：总调用次数 + 各类 token。

    比 stats_summary(since_ts=0) 更轻：直接列 logs/*.db 文件，不做空月空转，
    也不查 retry_chain / recent_calls 等附加字段。
    """
    out = {
        "total": 0, "success_count": 0, "error_count": 0,
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation": 0, "cache_read": 0,
    }
    if _log_dir is None:
        return out
    if not os.path.isdir(_log_dir):
        return out
    current_path, _ = _current_db_path()
    for name in sorted(os.listdir(_log_dir)):
        if not name.endswith(".db"):
            continue
        path = os.path.join(_log_dir, name)
        # 当月 db 用 thread-local 可写连接；其它月只读打开
        if path == current_path:
            conn = _get_conn()
            close_fn = None
        else:
            try:
                uri = f"file:{path}?mode=ro"
                conn = sqlite3.connect(uri, uri=True, timeout=10)
                conn.row_factory = sqlite3.Row
            except Exception:
                continue
            close_fn = conn.close
        try:
            row = conn.execute(
                """SELECT
                     COUNT(*) AS total,
                     SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS succ,
                     SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS err,
                     SUM(input_tokens) AS inp,
                     SUM(output_tokens) AS outp,
                     SUM(cache_creation_tokens) AS cc,
                     SUM(cache_read_tokens) AS cr
                   FROM request_log""",
            ).fetchone()
            if row:
                out["total"] += int(row["total"] or 0)
                out["success_count"] += int(row["succ"] or 0)
                out["error_count"] += int(row["err"] or 0)
                out["input_tokens"] += int(row["inp"] or 0)
                out["output_tokens"] += int(row["outp"] or 0)
                out["cache_creation"] += int(row["cc"] or 0)
                out["cache_read"] += int(row["cr"] or 0)
        except Exception as exc:
            print(f"[log_db] stats_lifetime: {name} skipped: {exc}")
        finally:
            if close_fn is not None:
                try:
                    close_fn()
                except Exception:
                    pass
    return out


def tokens_for_channel(channel_key: str, since_ts: float) -> dict:
    """跨月聚合某 channel_key 在 since_ts 之后的 token 统计。

    用于 OAuth 列表里按账号显示"月度统计"等场景。
    返回 {total, input, output, cache_creation, cache_read}（int）。
    """
    out = {"total": 0, "input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
    if _log_dir is None or not os.path.isdir(_log_dir):
        return out

    current_path, _ = _current_db_path()
    start_dt = datetime.fromtimestamp(since_ts, tz=_BJT)
    now_dt = datetime.now(_BJT)

    cursor = (start_dt.year, start_dt.month)
    end = (now_dt.year, now_dt.month)
    while cursor <= end:
        y, m = cursor
        path = os.path.join(_log_dir, f"{y:04d}-{m:02d}.db")
        if cursor == (now_dt.year, now_dt.month) and path == current_path:
            conn = _get_conn()
            close_fn = None
        elif os.path.exists(path):
            try:
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
                conn.row_factory = sqlite3.Row
            except Exception:
                conn = None
                close_fn = None
            else:
                close_fn = conn.close
        else:
            conn = None
            close_fn = None

        if conn is not None:
            try:
                row = conn.execute(
                    """SELECT
                         COUNT(*) AS total,
                         SUM(input_tokens) AS inp,
                         SUM(output_tokens) AS outp,
                         SUM(cache_creation_tokens) AS cc,
                         SUM(cache_read_tokens) AS cr
                       FROM request_log
                       WHERE final_channel_key=? AND created_at >= ?""",
                    (channel_key, since_ts),
                ).fetchone()
                if row:
                    out["total"] += int(row["total"] or 0)
                    out["input"] += int(row["inp"] or 0)
                    out["output"] += int(row["outp"] or 0)
                    out["cache_creation"] += int(row["cc"] or 0)
                    out["cache_read"] += int(row["cr"] or 0)
            except Exception as exc:
                print(f"[log_db] tokens_for_channel: skip {path}: {exc}")
            finally:
                if close_fn is not None:
                    try:
                        close_fn()
                    except Exception:
                        pass

        # 下一月
        if m == 12:
            cursor = (y + 1, 1)
        else:
            cursor = (y, m + 1)
    return out


def cleanup_stale_pending(timeout_seconds: int = 1800) -> int:
    cutoff = time.time() - timeout_seconds
    with _write_lock:
        cur = _get_conn().execute(
            """UPDATE request_log
               SET status='error',
                   error_message='process crashed (stale pending)',
                   finished_at=?
               WHERE status='pending' AND created_at < ?""",
            (time.time(), cutoff),
        )
        _get_conn().commit()
        return cur.rowcount


# ─── 查询 ──────────────────────────────────────────────────────────

_RECENT_COLS = (
    "request_id, created_at, api_key_name, requested_model, "
    "final_channel_key, final_channel_type, final_model, "
    "status, http_status, error_message, is_stream, "
    "input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, "
    "connect_time_ms, first_token_time_ms, total_time_ms, "
    "retry_count, affinity_hit"
)


def recent_logs(
    limit: int = 20,
    channel_key: str | None = None,
    model: str | None = None,
    status: str | None = None,
) -> list[dict]:
    conds, vals = [], []
    if channel_key:
        conds.append("final_channel_key=?"); vals.append(channel_key)
    if model:
        conds.append("(requested_model=? OR final_model=?)"); vals.extend([model, model])
    if status:
        conds.append("status=?"); vals.append(status)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    sql = f"SELECT {_RECENT_COLS} FROM request_log {where} ORDER BY created_at DESC LIMIT ?"
    vals.append(limit)
    rows = _get_conn().execute(sql, vals).fetchall()
    return [dict(r) for r in rows]


def log_detail(request_id: str) -> dict:
    log_row = _get_conn().execute(
        "SELECT * FROM request_log WHERE request_id=?", (request_id,),
    ).fetchone()
    detail_row = _get_conn().execute(
        "SELECT request_headers, request_body, response_body FROM request_detail WHERE request_id=?",
        (request_id,),
    ).fetchone()
    chain_rows = _get_conn().execute(
        "SELECT * FROM retry_chain WHERE request_id=? ORDER BY attempt_order ASC",
        (request_id,),
    ).fetchall()
    return {
        "log": dict(log_row) if log_row else None,
        "detail": dict(detail_row) if detail_row else None,
        "retry_chain": [dict(r) for r in chain_rows],
    }


def _iter_month_conns(since_ts: float):
    """返回 [(conn, close_fn)]，覆盖 since_ts..now 的所有月份（含当月）。"""
    current_path, current_month = _current_db_path()
    conns: list = []
    # 当月：用 thread-local 连接（可读可写）
    conns.append((_get_conn(), lambda: None))
    # 跨月：从 since_ts 月到上月（**不含**当月，当月已由上面的连接负责）
    start_dt = datetime.fromtimestamp(since_ts, tz=_BJT)
    now_dt = datetime.now(_BJT)
    cursor = (start_dt.year, start_dt.month)
    current = (now_dt.year, now_dt.month)
    while cursor < current:
        y, mm = cursor
        m = f"{y:04d}-{mm:02d}"
        c = _get_conn_for_month(m)
        if c is not None:
            conns.append((c, c.close))
        # 下一月
        if mm == 12:
            cursor = (y + 1, 1)
        else:
            cursor = (y, mm + 1)
    return conns


_GROUP_BY_COLS = {
    "channel": "COALESCE(final_channel_key, '?')",
    "model":   "COALESCE(requested_model, '?')",
    "apikey":  "COALESCE(api_key_name, '?')",
}


def stats_summary(
    since_ts: float,
    group_by: str | None = None,
    summary_top_limit: int = 3,
    group_limit: int = 10,
) -> dict:
    """跨月统计聚合。

    返回结构：
      {
        "overall": {汇总字段},
        "by_channel": [{"key": str, "metrics": {...}}, ...],   # group_by=None: top {summary_top_limit}
        "by_model":   [...],                                    # group_by="model": 展开 top {group_limit}
        "by_apikey":  [...],
        "recent_errors":       [...],   # 5 条
        "recent_calls":        [...],   # 3 条
        "recent_cache_misses": [...],   # 3 条（status=success 且 cache_read_tokens=0）
      }

    group_by 决定哪个维度展开到 group_limit；其它两个维度保持 summary_top_limit。
    group_by=None 时三个维度都只取 summary_top_limit。
    """
    conns = _iter_month_conns(since_ts)
    overall_agg = _new_overall_agg()
    by_channel: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_apikey: dict[str, dict] = {}
    recent_errors: list[dict] = []
    recent_calls: list[dict] = []
    recent_cache_misses: list[dict] = []

    def _agg_group(target: dict, conn, col_expr: str) -> None:
        rows = conn.execute(
            f"""SELECT {col_expr} AS grp_key,
                 COUNT(*) AS total,
                 SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
                 SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS error_count,
                 SUM(CASE WHEN status='success' AND cache_read_tokens > 0 THEN 1 ELSE 0 END) AS hit_requests,
                 SUM(CASE WHEN status='success' AND cache_creation_tokens > 0 THEN 1 ELSE 0 END) AS write_requests,
                 SUM(input_tokens + cache_creation_tokens + cache_read_tokens) AS total_prompt_tokens,
                 SUM(output_tokens) AS total_output_tokens,
                 SUM(cache_creation_tokens) AS total_cache_creation,
                 SUM(cache_read_tokens) AS total_cache_read,
                 SUM(CASE WHEN status='success' AND connect_time_ms IS NOT NULL THEN connect_time_ms ELSE 0 END) AS sum_connect_ms,
                 SUM(CASE WHEN status='success' AND connect_time_ms IS NOT NULL THEN 1 ELSE 0 END) AS cnt_connect,
                 SUM(CASE WHEN status='success' AND is_stream=1 AND first_token_time_ms IS NOT NULL THEN first_token_time_ms ELSE 0 END) AS sum_first_token_ms,
                 SUM(CASE WHEN status='success' AND is_stream=1 AND first_token_time_ms IS NOT NULL THEN 1 ELSE 0 END) AS cnt_first_token
               FROM request_log WHERE created_at >= ?
               GROUP BY grp_key""",
            (since_ts,),
        ).fetchall()
        for r in rows:
            k = r["grp_key"] or "?"
            bucket = target.setdefault(k, _new_group_agg())
            _accumulate_group(bucket, r)

    try:
        for conn, _ in conns:
            row = conn.execute(
                """SELECT
                     COUNT(*) AS total,
                     SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
                     SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS error_count,
                     SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending_count,
                     SUM(retry_count) AS total_retries,
                     SUM(CASE WHEN retry_count > 0 THEN 1 ELSE 0 END) AS retried_requests,
                     SUM(CASE WHEN affinity_hit=1 THEN 1 ELSE 0 END) AS affinity_hits,
                     SUM(CASE WHEN status='success' AND cache_read_tokens > 0 THEN 1 ELSE 0 END) AS success_with_cache_hit,
                     SUM(CASE WHEN status='success' AND cache_creation_tokens > 0 THEN 1 ELSE 0 END) AS success_with_cache_write,
                     SUM(input_tokens) AS total_input_tokens,
                     SUM(output_tokens) AS total_output_tokens,
                     SUM(cache_creation_tokens) AS total_cache_creation,
                     SUM(cache_read_tokens) AS total_cache_read,
                     SUM(CASE WHEN status='success' AND connect_time_ms IS NOT NULL THEN connect_time_ms ELSE 0 END) AS sum_connect_ms,
                     SUM(CASE WHEN status='success' AND connect_time_ms IS NOT NULL THEN 1 ELSE 0 END) AS cnt_connect,
                     SUM(CASE WHEN status='success' AND is_stream=1 AND first_token_time_ms IS NOT NULL THEN first_token_time_ms ELSE 0 END) AS sum_first_token_ms,
                     SUM(CASE WHEN status='success' AND is_stream=1 AND first_token_time_ms IS NOT NULL THEN 1 ELSE 0 END) AS cnt_first_token,
                     SUM(CASE WHEN status='success' AND total_time_ms IS NOT NULL THEN total_time_ms ELSE 0 END) AS sum_total_ms,
                     SUM(CASE WHEN status='success' AND total_time_ms IS NOT NULL THEN 1 ELSE 0 END) AS cnt_total
                   FROM request_log WHERE created_at >= ?""",
                (since_ts,),
            ).fetchone()
            _accumulate(overall_agg, row)

            # 永远聚合三个维度（cc-proxy 风格：汇总视图也展示三方 Top）
            _agg_group(by_channel, conn, _GROUP_BY_COLS["channel"])
            _agg_group(by_model,   conn, _GROUP_BY_COLS["model"])
            _agg_group(by_apikey,  conn, _GROUP_BY_COLS["apikey"])

            for r in conn.execute(
                """SELECT created_at, api_key_name, requested_model,
                          final_channel_key, error_message
                   FROM request_log WHERE status='error' AND created_at >= ?
                   ORDER BY created_at DESC LIMIT 5""",
                (since_ts,),
            ).fetchall():
                recent_errors.append(dict(r))

            for r in conn.execute(
                f"""SELECT {_RECENT_COLS}
                   FROM request_log WHERE created_at >= ?
                   ORDER BY created_at DESC LIMIT 3""",
                (since_ts,),
            ).fetchall():
                recent_calls.append(dict(r))

            # 最近未命中样本（cc-proxy 同款）：成功但 cache_read_tokens=0
            for r in conn.execute(
                """SELECT request_id, created_at, api_key_name, requested_model,
                          final_channel_key, is_stream, msg_count, tool_count,
                          input_tokens, output_tokens,
                          cache_creation_tokens, cache_read_tokens,
                          connect_time_ms, first_token_time_ms, total_time_ms,
                          retry_count, affinity_hit
                   FROM request_log
                   WHERE created_at >= ? AND status='success' AND cache_read_tokens=0
                   ORDER BY created_at DESC LIMIT 3""",
                (since_ts,),
            ).fetchall():
                recent_cache_misses.append(dict(r))
    finally:
        for conn, close_fn in conns:
            try:
                close_fn()
            except Exception:
                pass

    recent_errors.sort(key=lambda r: r["created_at"], reverse=True)
    recent_errors = recent_errors[:5]
    recent_calls.sort(key=lambda r: r["created_at"], reverse=True)
    recent_calls = recent_calls[:3]
    recent_cache_misses.sort(key=lambda r: r["created_at"], reverse=True)
    recent_cache_misses = recent_cache_misses[:3]

    def _finalize_dim(agg: dict[str, dict], top: int) -> list[dict]:
        out = [{"key": k, "metrics": _finalize_group(v)} for k, v in agg.items()]
        out.sort(key=lambda g: g["metrics"]["total_prompt_tokens"] or 0, reverse=True)
        return out[:top]

    # 按 group_by 决定每个维度的展开数量
    channel_top = group_limit if group_by == "channel" else summary_top_limit
    model_top   = group_limit if group_by == "model"   else summary_top_limit
    apikey_top  = group_limit if group_by == "apikey"  else summary_top_limit

    return {
        "overall": _finalize_overall(overall_agg),
        "by_channel": _finalize_dim(by_channel, channel_top),
        "by_model":   _finalize_dim(by_model,   model_top),
        "by_apikey":  _finalize_dim(by_apikey,  apikey_top),
        "recent_errors": recent_errors,
        "recent_calls": recent_calls,
        "recent_cache_misses": recent_cache_misses,
    }


# ─── 聚合辅助 ──────────────────────────────────────────────────────

_OVERALL_FIELDS = [
    "total", "success_count", "error_count", "pending_count",
    "total_retries", "retried_requests", "affinity_hits",
    "success_with_cache_hit", "success_with_cache_write",
    "total_input_tokens", "total_output_tokens",
    "total_cache_creation", "total_cache_read",
    "sum_connect_ms", "cnt_connect",
    "sum_first_token_ms", "cnt_first_token",
    "sum_total_ms", "cnt_total",
]


def _new_overall_agg() -> dict:
    return {k: 0 for k in _OVERALL_FIELDS}


def _accumulate(agg: dict, row) -> None:
    for k in _OVERALL_FIELDS:
        agg[k] = (agg.get(k) or 0) + (row[k] or 0)


def _finalize_overall(agg: dict) -> dict:
    def _avg(s, c):
        return (s / c) if c > 0 else None
    return {
        "total": agg["total"],
        "success_count": agg["success_count"],
        "error_count": agg["error_count"],
        "pending_count": agg["pending_count"],
        "total_retries": agg["total_retries"],
        "retried_requests": agg["retried_requests"],
        "affinity_hits": agg["affinity_hits"],
        "success_with_cache_hit": agg["success_with_cache_hit"],
        "success_with_cache_write": agg["success_with_cache_write"],
        "total_input_tokens": agg["total_input_tokens"],
        "total_output_tokens": agg["total_output_tokens"],
        "total_cache_creation": agg["total_cache_creation"],
        "total_cache_read": agg["total_cache_read"],
        "avg_connect_ms": _avg(agg["sum_connect_ms"], agg["cnt_connect"]),
        "avg_first_token_ms": _avg(agg["sum_first_token_ms"], agg["cnt_first_token"]),
        "avg_total_ms": _avg(agg["sum_total_ms"], agg["cnt_total"]),
    }


_GROUP_FIELDS = [
    "total", "success_count", "error_count",
    "hit_requests", "write_requests",
    "total_prompt_tokens", "total_output_tokens",
    "total_cache_creation", "total_cache_read",
    "sum_connect_ms", "cnt_connect",
    "sum_first_token_ms", "cnt_first_token",
]


def _new_group_agg() -> dict:
    return {k: 0 for k in _GROUP_FIELDS}


def _accumulate_group(agg: dict, row) -> None:
    for k in _GROUP_FIELDS:
        agg[k] = (agg.get(k) or 0) + (row[k] or 0)


def _finalize_group(agg: dict) -> dict:
    out = dict(agg)
    out["avg_connect_ms"] = (
        out["sum_connect_ms"] / out["cnt_connect"] if out["cnt_connect"] > 0 else None
    )
    out["avg_first_token_ms"] = (
        out["sum_first_token_ms"] / out["cnt_first_token"] if out["cnt_first_token"] > 0 else None
    )
    return out
