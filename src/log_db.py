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
      fingerprint           TEXT,
      -- 入口协议：anthropic（/v1/messages）/ chat / responses。insert_pending 阶段确定。
      ingress_protocol      TEXT,
      -- 选中渠道的上游协议：anthropic / openai-chat / openai-responses。finish_* 阶段确定。
      upstream_protocol     TEXT
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


def _ensure_migrations(conn: sqlite3.Connection) -> None:
    """对已存在的 request_log 表按需追加新列（老月份 DB 升级入口）。

    SQLite ADD COLUMN 无 IF NOT EXISTS 语法，需要先查 PRAGMA table_info。
    本函数调用方必须在持 `_write_lock` 的前提下调用；幂等，老列齐全时零开销。
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(request_log)").fetchall()}
    changed = False
    if "ingress_protocol" not in cols:
        conn.execute("ALTER TABLE request_log ADD COLUMN ingress_protocol TEXT")
        changed = True
    if "upstream_protocol" not in cols:
        conn.execute("ALTER TABLE request_log ADD COLUMN upstream_protocol TEXT")
        changed = True
    if changed:
        conn.commit()


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
            _ensure_migrations(conn)
            conn.commit()
        _local.conn = conn
        _local.month = month
    return _local.conn


def _get_conn_for_month(month: str) -> sqlite3.Connection | None:
    """打开指定月份的 DB 用于只读查询；不存在返回 None。

    打开方式改为读写（非 `?mode=ro`）以便 `_ensure_migrations` 能为老 DB
    追加新列。查询层仍按只读使用，没有 INSERT/UPDATE 路径进入。
    """
    if _log_dir is None:
        return None
    path = os.path.join(_log_dir, f"{month}.db")
    if not os.path.exists(path):
        return None
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    with _write_lock:
        _ensure_migrations(conn)
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
    ingress_protocol: str = "anthropic",
) -> None:
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            """INSERT INTO request_log
               (request_id, created_at, client_ip, api_key_name, requested_model,
                status, is_stream, msg_count, tool_count, fingerprint,
                ingress_protocol)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                request_id, time.time(), client_ip, api_key_name, requested_model,
                "pending", 1 if is_stream else 0, msg_count, tool_count,
                # log 表只存 16 字符（节省空间）；它是 affinity 表 32 字符指纹的前缀，
                # 排查时可用 `log.fingerprint || '%'` 做前缀匹配反查 cache_affinities。
                fingerprint[:16] if fingerprint else None,
                ingress_protocol or "anthropic",
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
    upstream_protocol: str | None = None,
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
                 retry_count=?, affinity_hit=?, upstream_protocol=?
               WHERE request_id=?""",
            (
                time.time(), http_status,
                final_channel_key, final_channel_type, final_model,
                input_tokens, output_tokens,
                cache_creation_tokens, cache_read_tokens,
                connect_ms, first_token_ms, total_ms,
                retry_count, affinity_hit, upstream_protocol,
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
    upstream_protocol: str | None = None,
) -> None:
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            """UPDATE request_log SET
                 status='error', finished_at=?, error_message=?, http_status=?,
                 final_channel_key=?, final_channel_type=?, final_model=?,
                 connect_time_ms=?, first_token_time_ms=?, total_time_ms=?,
                 retry_count=?, affinity_hit=?, upstream_protocol=?
               WHERE request_id=?""",
            (
                time.time(), error_message, http_status,
                final_channel_key, final_channel_type, final_model,
                connect_ms, first_token_ms, total_ms,
                retry_count, affinity_hit, upstream_protocol,
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
    """跨月聚合某 channel_key 在 since_ts 之后的统计（含 TPS）。

    返回字段：
      - total / success_count / error_count          次数
      - input / output / cache_creation / cache_read tokens
      - avg_tps / max_tps / min_tps                  生成速度（可能为 None）
    """
    return _aggregate_by_filter(
        "final_channel_key=?", (channel_key,), since_ts,
    )


def tokens_for_apikey(api_key_name: str, since_ts: float) -> dict:
    """跨月聚合某 API Key 在 since_ts 之后的统计（字段同 tokens_for_channel）。"""
    return _aggregate_by_filter(
        "api_key_name=?", (api_key_name,), since_ts,
    )


def _aggregate_by_filter(where: str, where_args: tuple, since_ts: float) -> dict:
    """按给定 WHERE 条件跨月聚合；where 不含 created_at 过滤。"""
    out: dict[str, Any] = {
        "total": 0, "success_count": 0, "error_count": 0,
        "input": 0, "output": 0, "cache_creation": 0, "cache_read": 0,
        "tps_num_tokens": 0, "tps_denom_ms": 0,
        "max_tps": None, "min_tps": None,
    }
    if _log_dir is None or not os.path.isdir(_log_dir):
        return _pack_stats(out)

    for conn, close_fn in _iter_month_conns_all(since_ts):
        try:
            row = conn.execute(
                f"""SELECT
                     COUNT(*) AS total,
                     SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
                     SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS error_count,
                     SUM(input_tokens) AS inp,
                     SUM(output_tokens) AS outp,
                     SUM(cache_creation_tokens) AS cc,
                     SUM(cache_read_tokens) AS cr,
                     {_tps_agg_sql()}
                   FROM request_log
                   WHERE {where} AND created_at >= ?""",
                where_args + (since_ts,),
            ).fetchone()
            if row:
                out["total"] += int(row["total"] or 0)
                out["success_count"] += int(row["success_count"] or 0)
                out["error_count"] += int(row["error_count"] or 0)
                out["input"] += int(row["inp"] or 0)
                out["output"] += int(row["outp"] or 0)
                out["cache_creation"] += int(row["cc"] or 0)
                out["cache_read"] += int(row["cr"] or 0)
                _merge_tps(out, row)
        except Exception as exc:
            print(f"[log_db] _aggregate_by_filter: skip: {exc}")
        finally:
            try:
                close_fn()
            except Exception:
                pass
    return _pack_stats(out)


def _pack_stats(raw: dict) -> dict:
    """把 _aggregate_by_filter 的内部累加结构 finalize 为对外格式。"""
    tps = _finalize_tps(raw)
    return {
        "total": raw["total"],
        "success_count": raw["success_count"],
        "error_count": raw["error_count"],
        "input": raw["input"],
        "output": raw["output"],
        "cache_creation": raw["cache_creation"],
        "cache_read": raw["cache_read"],
        "avg_tps": tps["avg_tps"],
        "max_tps": tps["max_tps"],
        "min_tps": tps["min_tps"],
    }


def channel_model_stats(channel_key: str, since_ts: float) -> list[dict]:
    """跨月按 final_model 分组聚合某渠道下每个模型的统计（含 TPS）。

    用于渠道详情/ OAuth 账户详情的"按模型展开"视图。
    每条 dict 含 final_model + tokens_for_channel 的所有字段。
    """
    by_model: dict[str, dict] = {}
    if _log_dir is None or not os.path.isdir(_log_dir):
        return []

    for conn, close_fn in _iter_month_conns_all(since_ts):
        try:
            rows = conn.execute(
                f"""SELECT
                     COALESCE(final_model, '?') AS model,
                     COUNT(*) AS total,
                     SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
                     SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS error_count,
                     SUM(input_tokens) AS inp,
                     SUM(output_tokens) AS outp,
                     SUM(cache_creation_tokens) AS cc,
                     SUM(cache_read_tokens) AS cr,
                     {_tps_agg_sql()}
                   FROM request_log
                   WHERE final_channel_key=? AND created_at >= ?
                   GROUP BY COALESCE(final_model, '?')""",
                (channel_key, since_ts),
            ).fetchall()
            for r in rows:
                key = r["model"] or "?"
                bucket = by_model.setdefault(key, {
                    "total": 0, "success_count": 0, "error_count": 0,
                    "input": 0, "output": 0, "cache_creation": 0, "cache_read": 0,
                    "tps_num_tokens": 0, "tps_denom_ms": 0,
                    "max_tps": None, "min_tps": None,
                })
                bucket["total"] += int(r["total"] or 0)
                bucket["success_count"] += int(r["success_count"] or 0)
                bucket["error_count"] += int(r["error_count"] or 0)
                bucket["input"] += int(r["inp"] or 0)
                bucket["output"] += int(r["outp"] or 0)
                bucket["cache_creation"] += int(r["cc"] or 0)
                bucket["cache_read"] += int(r["cr"] or 0)
                _merge_tps(bucket, r)
        except Exception as exc:
            print(f"[log_db] channel_model_stats: skip: {exc}")
        finally:
            try:
                close_fn()
            except Exception:
                pass

    out = []
    for model, raw in by_model.items():
        d = _pack_stats(raw)
        d["final_model"] = model
        out.append(d)
    # 按请求量降序；方便 UI 直接渲染
    out.sort(key=lambda x: x["total"], reverse=True)
    return out


def channels_by_requested_model(since_ts: float) -> dict[str, list[dict]]:
    """跨月按 requested_model 分组，汇总每个模型实际落到的 (渠道, 渠道类型) 列表。

    返回 {requested_model: [{"key": "...", "type": "api|oauth", "count": n}, ...]}，
    内部按 count 降序。用于「按模型 Top」展示"所属渠道"。
    """
    acc: dict[str, dict[tuple[str, str], int]] = {}
    if _log_dir is None or not os.path.isdir(_log_dir):
        return {}
    for conn, close_fn in _iter_month_conns_all(since_ts):
        try:
            rows = conn.execute(
                """SELECT COALESCE(requested_model, '?') AS model,
                          COALESCE(final_channel_key, '?') AS ck,
                          COALESCE(final_channel_type, '?') AS ct,
                          COUNT(*) AS cnt
                     FROM request_log
                    WHERE created_at >= ?
                      AND final_channel_key IS NOT NULL
                    GROUP BY model, ck, ct""",
                (since_ts,),
            ).fetchall()
            for r in rows:
                model = r["model"]
                bucket = acc.setdefault(model, {})
                k = (r["ck"], r["ct"])
                bucket[k] = bucket.get(k, 0) + int(r["cnt"] or 0)
        except Exception as exc:
            print(f"[log_db] channels_by_requested_model: skip: {exc}")
        finally:
            try:
                close_fn()
            except Exception:
                pass

    out: dict[str, list[dict]] = {}
    for model, mapping in acc.items():
        items = [{"key": k, "type": t, "count": n} for (k, t), n in mapping.items()]
        items.sort(key=lambda x: x["count"], reverse=True)
        out[model] = items
    return out


def tps_by_channel_model(since_ts: float) -> dict[tuple[str, str], float]:
    """跨月聚合 {(channel_key, model): avg_tps}，用于"最快渠道"区 lookup。"""
    acc: dict[tuple[str, str], dict] = {}
    if _log_dir is None or not os.path.isdir(_log_dir):
        return {}
    for conn, close_fn in _iter_month_conns_all(since_ts):
        try:
            rows = conn.execute(
                f"""SELECT final_channel_key AS ck, final_model AS m,
                     {_tps_agg_sql()}
                   FROM request_log
                   WHERE final_channel_key IS NOT NULL AND final_model IS NOT NULL
                     AND created_at >= ?
                   GROUP BY final_channel_key, final_model""",
                (since_ts,),
            ).fetchall()
            for r in rows:
                key = (r["ck"], r["m"])
                bucket = acc.setdefault(key, {
                    "tps_num_tokens": 0, "tps_denom_ms": 0,
                    "max_tps": None, "min_tps": None,
                })
                _merge_tps(bucket, r)
        except Exception as exc:
            print(f"[log_db] tps_by_channel_model: skip: {exc}")
        finally:
            try:
                close_fn()
            except Exception:
                pass

    return {
        k: _finalize_tps(v)["avg_tps"]
        for k, v in acc.items()
        if _finalize_tps(v)["avg_tps"] is not None
    }


def _iter_month_conns_all(since_ts: float):
    """从 since_ts 所在月起到当月止，产出 (conn, close_fn) 序列。

    与 _iter_month_conns 不同的是：后者假设每个 conn 只会在同一轮被用；
    这里每个 conn 可能跨多个查询（channel_model_stats 等），所以统一 close。
    当月连接用 thread-local 共享连接，close_fn=noop。
    """
    if _log_dir is None:
        return
    current_path, _ = _current_db_path()
    start_dt = datetime.fromtimestamp(since_ts, tz=_BJT)
    now_dt = datetime.now(_BJT)

    cursor = (start_dt.year, start_dt.month)
    end = (now_dt.year, now_dt.month)
    while cursor <= end:
        y, m = cursor
        path = os.path.join(_log_dir, f"{y:04d}-{m:02d}.db")
        if cursor == end and path == current_path:
            yield _get_conn(), lambda: None
        elif os.path.exists(path):
            try:
                # 打开为读写：读路径不会 INSERT/UPDATE，但 _ensure_migrations 需要
                # ALTER TABLE 能力给老月份 DB 补列（如新增 ingress_protocol）。
                c = sqlite3.connect(path, timeout=10)
                c.row_factory = sqlite3.Row
                c.execute("PRAGMA busy_timeout=5000")
                with _write_lock:
                    _ensure_migrations(c)
                yield c, c.close
            except Exception:
                pass
        if m == 12:
            cursor = (y + 1, 1)
        else:
            cursor = (y, m + 1)


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


# 家族过滤 SQL 片段：当 family 指定时返回 `AND upstream_protocol IN (...)`，否则空串。
# upstream_protocol 由 finish_success / finish_error 写入；pending / 未落盘的请求
# 归类会缺失（它们也确实没真正产生流量），因此家族聚合的 total 会比不过滤时略小。
_FAMILY_UPSTREAM: dict[str, tuple[str, ...]] = {
    "anthropic": ("anthropic",),
    "openai":    ("openai-chat", "openai-responses"),
}


def _family_where(family: str | None) -> str:
    if not family or family not in _FAMILY_UPSTREAM:
        return ""
    protos = _FAMILY_UPSTREAM[family]
    placeholders = ",".join("?" * len(protos))
    return f" AND upstream_protocol IN ({placeholders})"


def _family_params(family: str | None) -> tuple:
    if not family or family not in _FAMILY_UPSTREAM:
        return ()
    return _FAMILY_UPSTREAM[family]

_RECENT_COLS = (
    "request_id, created_at, api_key_name, requested_model, "
    "final_channel_key, final_channel_type, final_model, "
    "status, http_status, error_message, is_stream, "
    "input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, "
    "connect_time_ms, first_token_time_ms, total_time_ms, "
    "retry_count, affinity_hit, "
    "ingress_protocol, upstream_protocol"
)


# ─── 每秒生成 tokens (TPS) 的 SQL 片段 ─────────────────────────────
# 口径：成功请求；stream 有 first_token_time_ms 时取生成阶段（total-first）；
#      非 stream 回退整体耗时。聚合用"加权平均"= Σtokens / Σdenom_ms × 1000。
_TPS_COND = (
    "status='success' AND output_tokens > 0 AND ("
    "(is_stream=1 AND first_token_time_ms IS NOT NULL "
    "AND total_time_ms > first_token_time_ms) "
    "OR (is_stream=0 AND total_time_ms > 0))"
)
_TPS_DENOM_MS_EXPR = (
    "CASE WHEN is_stream=1 AND first_token_time_ms IS NOT NULL "
    "AND total_time_ms > first_token_time_ms "
    "THEN (total_time_ms - first_token_time_ms) "
    "ELSE total_time_ms END"
)
_TPS_VALUE_EXPR = f"(output_tokens*1000.0 / {_TPS_DENOM_MS_EXPR})"


def _tps_agg_sql() -> str:
    """返回 4 列聚合 SQL：tps_num_tokens / tps_denom_ms / max_tps / min_tps。
    调用方把它塞进 SELECT 列表里。"""
    return (
        f"SUM(CASE WHEN {_TPS_COND} THEN output_tokens ELSE 0 END) AS tps_num_tokens,\n"
        f"SUM(CASE WHEN {_TPS_COND} THEN {_TPS_DENOM_MS_EXPR} ELSE 0 END) AS tps_denom_ms,\n"
        f"MAX(CASE WHEN {_TPS_COND} THEN {_TPS_VALUE_EXPR} ELSE NULL END) AS max_tps,\n"
        f"MIN(CASE WHEN {_TPS_COND} THEN {_TPS_VALUE_EXPR} ELSE NULL END) AS min_tps"
    )


def _merge_tps(agg: dict, row) -> None:
    """把单条 SQL row 的 tps 聚合合并到 agg（跨月累加）。
    agg 维持 num_tokens / denom_ms / max_tps / min_tps 四个键。"""
    agg["tps_num_tokens"] = (agg.get("tps_num_tokens") or 0) + int(row["tps_num_tokens"] or 0)
    agg["tps_denom_ms"] = (agg.get("tps_denom_ms") or 0) + int(row["tps_denom_ms"] or 0)
    mt = row["max_tps"]
    if mt is not None:
        cur = agg.get("max_tps")
        agg["max_tps"] = float(mt) if cur is None else max(float(cur), float(mt))
    mn = row["min_tps"]
    if mn is not None:
        cur = agg.get("min_tps")
        agg["min_tps"] = float(mn) if cur is None else min(float(cur), float(mn))


def _finalize_tps(agg: dict) -> dict:
    """把 tps 聚合结构 finalize 为 {avg_tps, max_tps, min_tps}。"""
    denom = int(agg.get("tps_denom_ms") or 0)
    num = int(agg.get("tps_num_tokens") or 0)
    avg = (num * 1000.0 / denom) if denom > 0 else None
    return {
        "avg_tps": avg,
        "max_tps": agg.get("max_tps"),
        "min_tps": agg.get("min_tps"),
    }


def _recent_logs_where(
    channel_key: str | None = None,
    model: str | None = None,
    status: str | None = None,
) -> tuple[str, list]:
    conds, vals = [], []
    if channel_key:
        conds.append("final_channel_key=?"); vals.append(channel_key)
    if model:
        conds.append("(requested_model=? OR final_model=?)"); vals.extend([model, model])
    if status:
        conds.append("status=?"); vals.append(status)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return where, vals


def recent_logs(
    limit: int = 20,
    channel_key: str | None = None,
    model: str | None = None,
    status: str | None = None,
    offset: int = 0,
) -> list[dict]:
    where, vals = _recent_logs_where(channel_key, model, status)
    lim = max(1, int(limit or 20))
    off = max(0, int(offset or 0))
    sql = f"SELECT {_RECENT_COLS} FROM request_log {where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
    vals.extend([lim, off])
    rows = _get_conn().execute(sql, vals).fetchall()
    return [dict(r) for r in rows]


def recent_logs_count(
    channel_key: str | None = None,
    model: str | None = None,
    status: str | None = None,
) -> int:
    where, vals = _recent_logs_where(channel_key, model, status)
    row = _get_conn().execute(f"SELECT COUNT(*) AS n FROM request_log {where}", vals).fetchone()
    return int(row["n"] or 0) if row else 0


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
    family: str | None = None,
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
                 SUM(CASE WHEN status='success' AND is_stream=1 AND first_token_time_ms IS NOT NULL THEN 1 ELSE 0 END) AS cnt_first_token,
                 {_tps_agg_sql()}
               FROM request_log WHERE created_at >= ?{_family_where(family)}
               GROUP BY grp_key""",
            (since_ts, *_family_params(family)),
        ).fetchall()
        for r in rows:
            k = r["grp_key"] or "?"
            bucket = target.setdefault(k, _new_group_agg())
            _accumulate_group(bucket, r)
            _merge_tps(bucket, r)

    try:
        for conn, _ in conns:
            row = conn.execute(
                f"""SELECT
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
                     SUM(CASE WHEN status='success' AND total_time_ms IS NOT NULL THEN 1 ELSE 0 END) AS cnt_total,
                     {_tps_agg_sql()}
                   FROM request_log WHERE created_at >= ?{_family_where(family)}""",
                (since_ts, *_family_params(family)),
            ).fetchone()
            _accumulate(overall_agg, row)
            _merge_tps(overall_agg, row)

            # 永远聚合三个维度（cc-proxy 风格：汇总视图也展示三方 Top）
            _agg_group(by_channel, conn, _GROUP_BY_COLS["channel"])
            _agg_group(by_model,   conn, _GROUP_BY_COLS["model"])
            _agg_group(by_apikey,  conn, _GROUP_BY_COLS["apikey"])

            for r in conn.execute(
                """SELECT created_at, api_key_name, requested_model,
                          final_channel_key, error_message,
                          ingress_protocol, upstream_protocol
                   FROM request_log WHERE status='error' AND created_at >= ?{_family_where_sql}
                   ORDER BY created_at DESC LIMIT 5""".format(_family_where_sql=_family_where(family)),
                (since_ts, *_family_params(family)),
            ).fetchall():
                recent_errors.append(dict(r))

            for r in conn.execute(
                f"""SELECT {_RECENT_COLS}
                   FROM request_log WHERE created_at >= ?{_family_where(family)}
                   ORDER BY created_at DESC LIMIT 3""",
                (since_ts, *_family_params(family)),
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
                   WHERE created_at >= ?{_family_where_sql} AND status='success' AND cache_read_tokens=0
                   ORDER BY created_at DESC LIMIT 3""".format(_family_where_sql=_family_where(family)),
                (since_ts, *_family_params(family)),
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
    out = {
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
    out.update(_finalize_tps(agg))
    return out


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
    out.update(_finalize_tps(agg))
    return out
