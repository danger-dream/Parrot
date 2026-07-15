"""按月分库的业务日志。

文件名 logs/YYYY-MM.db，按北京时间判断月份。
三张表：
  - request_log      请求摘要（供统计与列表）
  - request_detail   大字段（headers / body / response_body）
  - retry_chain      重试链（每个渠道尝试一条记录）
  - proxy_chain      代理链（每个渠道尝试内的代理切换明细）
  - local_web_log    Parrot 本地 WebSearch/WebFetch 执行明细

写操作由 `_write_lock` 序列化；跨月自动切换连接。
"""

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from . import config

_BJT = timezone(timedelta(hours=8))
_local = threading.local()
# 可重入：_get_conn 在新线程首次创建连接时自身需持锁做 CREATE TABLE，
# 而上层写函数先取锁再调 _get_conn；非重入锁会死锁。
_write_lock = threading.RLock()
_initialized = False
_log_dir: str | None = None


@dataclass(frozen=True)
class LogDbRef:
    """A concrete monthly database identity captured when a row is created."""

    month: str
    path: str


@dataclass(frozen=True)
class RequestLogHandle:
    request_id: str
    db: LogDbRef


@dataclass(frozen=True)
class RowLogHandle:
    table: Literal["retry_chain", "proxy_chain", "local_web_log"]
    row_id: int
    request_id: str
    db: LogDbRef

    def __int__(self) -> int:
        """Compatibility for old tests/diagnostics; production updates use handle."""

        return self.row_id


class HistoricalLogError(RuntimeError):
    """A historical month couldn't be queried read-only and must not be skipped."""


# Compatibility lookup for call sites migrated incrementally.  The authoritative
# value is still the immutable handle returned by insert_pending; S5/S6 thread it
# explicitly through active request paths.
_request_handles: dict[str, RequestLogHandle] = {}


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
      -- Four business metrics from the final/terminal upstream route round.
      connect_time_ms       INTEGER,
      first_token_time_ms   INTEGER,
      idle_time_ms          INTEGER,
      total_time_ms         INTEGER,
      final_round_id        TEXT,
      -- Downstream/request lifecycle display only; never a business round total.
      request_elapsed_ms    INTEGER,
      request_upload_ms     INTEGER,
      response_headers_wait_ms INTEGER,
      response_body_first_byte_wait_ms INTEGER,
      retry_count           INTEGER DEFAULT 0,
      affinity_hit          INTEGER DEFAULT 0,
      fingerprint           TEXT,
      -- 入口协议：anthropic（/v1/messages）/ chat / responses。insert_pending 阶段确定。
      ingress_protocol      TEXT,
      -- 选中渠道的上游协议：anthropic / openai-chat / openai-responses。finish_* 阶段确定。
      upstream_protocol     TEXT,
      -- 上游传输层：http / ws。下游 WS 与 HTTP→WS 上游转换都用 ws 标记。
      upstream_transport    TEXT,
      -- 出站代理名称（proxy subsystem）。NULL / 空 = 直连。
      proxy_name            TEXT,
      -- 代理层字节数：统计经由该代理转发的上游 body bytes（请求 + 响应）。
      proxy_bytes_up        INTEGER DEFAULT 0,
      proxy_bytes_down      INTEGER DEFAULT 0,
      reasoning_effort      TEXT,
      fast_mode             INTEGER DEFAULT 0
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
      -- Final/terminal route-round summary for this channel attempt.
      final_round_id  TEXT,
      connect_ms      INTEGER,
      first_byte_ms   INTEGER,
      idle_ms         INTEGER,
      request_upload_ms INTEGER,
      response_headers_wait_ms INTEGER,
      response_body_first_byte_wait_ms INTEGER,
      total_ms        INTEGER,
      -- Outer channel-attempt display duration; never added to round total.
      attempt_elapsed_ms INTEGER,
      ended_at        REAL,
      outcome         TEXT,
      error_detail    TEXT,
      proxy_name      TEXT,
      bytes_up        INTEGER DEFAULT 0,
      bytes_down      INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_retry_req ON retry_chain(request_id);

    CREATE TABLE IF NOT EXISTS proxy_chain (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      request_id      TEXT NOT NULL,
      retry_attempt_id INTEGER,
      attempt_order   INTEGER NOT NULL,
      -- proxy_chain is the route-round detail table; direct is stored explicitly.
      round_id        TEXT,
      transport       TEXT,
      request_mode    TEXT,
      proxy_name      TEXT NOT NULL,
      started_at      REAL NOT NULL,
      connect_ms      INTEGER,
      first_byte_ms   INTEGER,
      idle_ms         INTEGER,
      total_ms        INTEGER,
      dns_ms          INTEGER,
      tcp_ms          INTEGER,
      proxy_tcp_ms    INTEGER,
      proxy_tunnel_ms INTEGER,
      tls_ms          INTEGER,
      target_tls_ms   INTEGER,
      ws_handshake_ms INTEGER,
      request_upload_ms INTEGER,
      response_headers_wait_ms INTEGER,
      response_body_first_byte_wait_ms INTEGER,
      ended_at        REAL,
      outcome         TEXT,
      error_detail    TEXT,
      bytes_up        INTEGER DEFAULT 0,
      bytes_down      INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_proxy_chain_req ON proxy_chain(request_id);

    CREATE TABLE IF NOT EXISTS local_web_log (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      request_id      TEXT NOT NULL,
      round_no        INTEGER DEFAULT 0,
      tool_name       TEXT NOT NULL,
      query           TEXT,
      url             TEXT,
      started_at      REAL NOT NULL,
      ended_at        REAL,
      status          TEXT,
      result_count    INTEGER DEFAULT 0,
      content_bytes   INTEGER DEFAULT 0,
      content_chars   INTEGER DEFAULT 0,
      error_message   TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_local_web_req ON local_web_log(request_id);
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


def _db_ref_for_timestamp(timestamp: float | None = None) -> LogDbRef:
    assert _log_dir is not None
    dt = datetime.now(_BJT) if timestamp is None else datetime.fromtimestamp(float(timestamp), tz=_BJT)
    month = dt.strftime("%Y-%m")
    return LogDbRef(month=month, path=os.path.join(_log_dir, f"{month}.db"))


def _current_db_path() -> tuple[str, str]:
    ref = _db_ref_for_timestamp()
    return ref.path, ref.month


def _request_handle(value: str | RequestLogHandle) -> RequestLogHandle:
    if isinstance(value, RequestLogHandle):
        return value
    with _write_lock:
        known = _request_handles.get(str(value))
    if known is not None:
        return known
    # Compatibility only for pre-handle callers that didn't originate through
    # insert_pending in this process.  New production paths pass the handle.
    return RequestLogHandle(request_id=str(value), db=_db_ref_for_timestamp())


def _row_handle(
    value: int | RowLogHandle,
    *,
    table: Literal["retry_chain", "proxy_chain", "local_web_log"],
) -> RowLogHandle:
    if isinstance(value, RowLogHandle):
        if value.table != table:
            raise ValueError(f"row handle table mismatch: {value.table!r} != {table!r}")
        return value
    # Legacy compatibility; production callers are migrated to RowLogHandle.
    return RowLogHandle(table=table, row_id=int(value), request_id="", db=_db_ref_for_timestamp())


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
    if "upstream_transport" not in cols:
        conn.execute("ALTER TABLE request_log ADD COLUMN upstream_transport TEXT")
        changed = True
    if "proxy_name" not in cols:
        conn.execute("ALTER TABLE request_log ADD COLUMN proxy_name TEXT")
        changed = True
    if "reasoning_effort" not in cols:
        conn.execute("ALTER TABLE request_log ADD COLUMN reasoning_effort TEXT")
        changed = True
    if "proxy_bytes_up" not in cols:
        conn.execute("ALTER TABLE request_log ADD COLUMN proxy_bytes_up INTEGER DEFAULT 0")
        changed = True
    if "proxy_bytes_down" not in cols:
        conn.execute("ALTER TABLE request_log ADD COLUMN proxy_bytes_down INTEGER DEFAULT 0")
        changed = True
    if "fast_mode" not in cols:
        conn.execute("ALTER TABLE request_log ADD COLUMN fast_mode INTEGER DEFAULT 0")
        changed = True
    for col in (
        "idle_time_ms",
        "request_elapsed_ms",
        "request_upload_ms",
        "response_headers_wait_ms",
        "response_body_first_byte_wait_ms",
    ):
        if col not in cols:
            conn.execute(f"ALTER TABLE request_log ADD COLUMN {col} INTEGER")
            changed = True
    if "final_round_id" not in cols:
        conn.execute("ALTER TABLE request_log ADD COLUMN final_round_id TEXT")
        changed = True
    # retry_chain migration
    retry_cols = {row[1] for row in conn.execute("PRAGMA table_info(retry_chain)").fetchall()}
    if retry_cols and "proxy_name" not in retry_cols:
        conn.execute("ALTER TABLE retry_chain ADD COLUMN proxy_name TEXT")
        changed = True
    if retry_cols and "bytes_up" not in retry_cols:
        conn.execute("ALTER TABLE retry_chain ADD COLUMN bytes_up INTEGER DEFAULT 0")
        changed = True
    if retry_cols and "bytes_down" not in retry_cols:
        conn.execute("ALTER TABLE retry_chain ADD COLUMN bytes_down INTEGER DEFAULT 0")
        changed = True
    if retry_cols:
        for col in (
            "idle_ms",
            "attempt_elapsed_ms",
            "request_upload_ms",
            "response_headers_wait_ms",
            "response_body_first_byte_wait_ms",
            "total_ms",
        ):
            if col not in retry_cols:
                conn.execute(f"ALTER TABLE retry_chain ADD COLUMN {col} INTEGER")
                changed = True
        if "final_round_id" not in retry_cols:
            conn.execute("ALTER TABLE retry_chain ADD COLUMN final_round_id TEXT")
            changed = True

    proxy_cols = {row[1] for row in conn.execute("PRAGMA table_info(proxy_chain)").fetchall()}
    if not proxy_cols:
        conn.execute("""CREATE TABLE IF NOT EXISTS proxy_chain (
          id              INTEGER PRIMARY KEY AUTOINCREMENT,
          request_id      TEXT NOT NULL,
          retry_attempt_id INTEGER,
          attempt_order   INTEGER NOT NULL,
          round_id        TEXT,
          transport       TEXT,
          request_mode    TEXT,
          proxy_name      TEXT NOT NULL,
          started_at      REAL NOT NULL,
          connect_ms      INTEGER,
          first_byte_ms   INTEGER,
          idle_ms         INTEGER,
          total_ms        INTEGER,
          dns_ms          INTEGER,
          tcp_ms          INTEGER,
          proxy_tcp_ms    INTEGER,
          proxy_tunnel_ms INTEGER,
          tls_ms          INTEGER,
          target_tls_ms   INTEGER,
          ws_handshake_ms INTEGER,
          request_upload_ms INTEGER,
          response_headers_wait_ms INTEGER,
          response_body_first_byte_wait_ms INTEGER,
          ended_at        REAL,
          outcome         TEXT,
          error_detail    TEXT,
          bytes_up        INTEGER DEFAULT 0,
          bytes_down      INTEGER DEFAULT 0
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_proxy_chain_req ON proxy_chain(request_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_proxy_chain_round ON proxy_chain(round_id)")
        changed = True
    else:
        for col, ddl in (
            ("retry_attempt_id", "ALTER TABLE proxy_chain ADD COLUMN retry_attempt_id INTEGER"),
            ("attempt_order", "ALTER TABLE proxy_chain ADD COLUMN attempt_order INTEGER DEFAULT 0"),
            ("round_id", "ALTER TABLE proxy_chain ADD COLUMN round_id TEXT"),
            ("transport", "ALTER TABLE proxy_chain ADD COLUMN transport TEXT"),
            ("request_mode", "ALTER TABLE proxy_chain ADD COLUMN request_mode TEXT"),
            ("connect_ms", "ALTER TABLE proxy_chain ADD COLUMN connect_ms INTEGER"),
            ("first_byte_ms", "ALTER TABLE proxy_chain ADD COLUMN first_byte_ms INTEGER"),
            ("idle_ms", "ALTER TABLE proxy_chain ADD COLUMN idle_ms INTEGER"),
            ("total_ms", "ALTER TABLE proxy_chain ADD COLUMN total_ms INTEGER"),
            ("dns_ms", "ALTER TABLE proxy_chain ADD COLUMN dns_ms INTEGER"),
            ("tcp_ms", "ALTER TABLE proxy_chain ADD COLUMN tcp_ms INTEGER"),
            ("proxy_tcp_ms", "ALTER TABLE proxy_chain ADD COLUMN proxy_tcp_ms INTEGER"),
            ("proxy_tunnel_ms", "ALTER TABLE proxy_chain ADD COLUMN proxy_tunnel_ms INTEGER"),
            ("tls_ms", "ALTER TABLE proxy_chain ADD COLUMN tls_ms INTEGER"),
            ("target_tls_ms", "ALTER TABLE proxy_chain ADD COLUMN target_tls_ms INTEGER"),
            ("ws_handshake_ms", "ALTER TABLE proxy_chain ADD COLUMN ws_handshake_ms INTEGER"),
            ("request_upload_ms", "ALTER TABLE proxy_chain ADD COLUMN request_upload_ms INTEGER"),
            ("response_headers_wait_ms", "ALTER TABLE proxy_chain ADD COLUMN response_headers_wait_ms INTEGER"),
            ("response_body_first_byte_wait_ms", "ALTER TABLE proxy_chain ADD COLUMN response_body_first_byte_wait_ms INTEGER"),
            ("bytes_up", "ALTER TABLE proxy_chain ADD COLUMN bytes_up INTEGER DEFAULT 0"),
            ("bytes_down", "ALTER TABLE proxy_chain ADD COLUMN bytes_down INTEGER DEFAULT 0"),
        ):
            if col not in proxy_cols:
                conn.execute(ddl)
                changed = True
        conn.execute("CREATE INDEX IF NOT EXISTS idx_proxy_chain_req ON proxy_chain(request_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_proxy_chain_round ON proxy_chain(round_id)")

    local_web_cols = {row[1] for row in conn.execute("PRAGMA table_info(local_web_log)").fetchall()}
    if not local_web_cols:
        conn.execute("""CREATE TABLE IF NOT EXISTS local_web_log (
          id              INTEGER PRIMARY KEY AUTOINCREMENT,
          request_id      TEXT NOT NULL,
          round_no        INTEGER DEFAULT 0,
          tool_name       TEXT NOT NULL,
          query           TEXT,
          url             TEXT,
          started_at      REAL NOT NULL,
          ended_at        REAL,
          status          TEXT,
          result_count    INTEGER DEFAULT 0,
          content_bytes   INTEGER DEFAULT 0,
          content_chars   INTEGER DEFAULT 0,
          error_message   TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_local_web_req ON local_web_log(request_id)")
        changed = True
    else:
        for col, ddl in (
            ("round_no", "ALTER TABLE local_web_log ADD COLUMN round_no INTEGER DEFAULT 0"),
            ("query", "ALTER TABLE local_web_log ADD COLUMN query TEXT"),
            ("url", "ALTER TABLE local_web_log ADD COLUMN url TEXT"),
            ("ended_at", "ALTER TABLE local_web_log ADD COLUMN ended_at REAL"),
            ("status", "ALTER TABLE local_web_log ADD COLUMN status TEXT"),
            ("result_count", "ALTER TABLE local_web_log ADD COLUMN result_count INTEGER DEFAULT 0"),
            ("content_bytes", "ALTER TABLE local_web_log ADD COLUMN content_bytes INTEGER DEFAULT 0"),
            ("content_chars", "ALTER TABLE local_web_log ADD COLUMN content_chars INTEGER DEFAULT 0"),
            ("error_message", "ALTER TABLE local_web_log ADD COLUMN error_message TEXT"),
        ):
            if col not in local_web_cols:
                conn.execute(ddl)
                changed = True
        conn.execute("CREATE INDEX IF NOT EXISTS idx_local_web_req ON local_web_log(request_id)")
    if changed:
        conn.commit()


def _get_conn_for_ref(ref: LogDbRef) -> sqlite3.Connection:
    """Return a write connection permanently bound to ``ref.path``.

    Connections are cached per thread *by path*, so a request that crosses the
    Beijing month boundary can still update its original DB.  We intentionally
    don't close the old-month connection just because the wall month changed.
    """

    if _log_dir is None:
        raise RuntimeError("log_db.init() not called")
    cache = getattr(_local, "write_conns", None)
    if cache is None:
        cache = {}
        _local.write_conns = cache
    conn = cache.get(ref.path)
    if conn is None:
        conn = sqlite3.connect(ref.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        with _write_lock:
            conn.executescript(_schema_sql())
            _ensure_migrations(conn)
            conn.commit()
        cache[ref.path] = conn
    # Legacy introspection compatibility only; write routing never reads these.
    _local.conn = conn
    _local.month = ref.month
    return conn


def _get_conn() -> sqlite3.Connection:
    """Return the current-month write connection for non-request maintenance."""

    return _get_conn_for_ref(_db_ref_for_timestamp())


def _open_readonly(path: str) -> sqlite3.Connection:
    try:
        uri = f"{Path(path).resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn
    except Exception as exc:
        raise HistoricalLogError(f"cannot open historical log read-only: {path}: {exc}") from exc


def _get_conn_for_month(month: str) -> sqlite3.Connection | None:
    """Open an existing month read-only; this function never runs migrations."""

    if _log_dir is None:
        return None
    path = os.path.join(_log_dir, f"{month}.db")
    if not os.path.exists(path):
        return None
    return _open_readonly(path)


def migrate_month_schema(month: str) -> None:
    """Explicit write migration entry; ordinary historical reads never call it."""

    if _log_dir is None:
        raise RuntimeError("log_db.init() not called")
    path = os.path.join(_log_dir, f"{month}.db")
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    conn = sqlite3.connect(path, timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        with _write_lock:
            conn.executescript(_schema_sql())
            _ensure_migrations(conn)
            conn.commit()
    finally:
        conn.close()


def checkpoint() -> None:
    try:
        _get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass


def migrate_channel_keys(mapping: dict[str, str]) -> dict:
    """Rename channel keys across all monthly log DBs.

    `mapping` maps full channel keys, e.g. `oauth:openai:email` →
    `oauth:openai:workspace`. Idempotent and best-effort for existing DB files.
    """
    stats = {"request_log_rows": 0, "retry_chain_rows": 0, "db_files": 0}
    if not mapping or _log_dir is None or not os.path.isdir(_log_dir):
        return stats

    for name in sorted(os.listdir(_log_dir)):
        if not name.endswith(".db"):
            continue
        path = os.path.join(_log_dir, name)
        try:
            conn = sqlite3.connect(path, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
        except Exception:
            continue
        try:
            with _write_lock:
                _ensure_migrations(conn)
                changed = 0
                for old_key, new_key in mapping.items():
                    if not old_key or not new_key or old_key == new_key:
                        continue
                    cur = conn.execute(
                        "UPDATE request_log SET final_channel_key=? WHERE final_channel_key=?",
                        (new_key, old_key),
                    )
                    stats["request_log_rows"] += int(cur.rowcount or 0)
                    changed += int(cur.rowcount or 0)
                    cur = conn.execute(
                        "UPDATE retry_chain SET channel_key=? WHERE channel_key=?",
                        (new_key, old_key),
                    )
                    stats["retry_chain_rows"] += int(cur.rowcount or 0)
                    changed += int(cur.rowcount or 0)
                if changed:
                    conn.commit()
                    stats["db_files"] += 1
        except Exception as exc:
            print(f"[log_db] channel key migration skipped {name}: {exc}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
    return stats


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
    reasoning_effort: str | None = None,
    fast_mode: bool | None = None,
    created_at: float | None = None,
) -> RequestLogHandle:
    created = time.time() if created_at is None else float(created_at)
    handle = RequestLogHandle(request_id=request_id, db=_db_ref_for_timestamp(created))
    with _write_lock:
        conn = _get_conn_for_ref(handle.db)
        conn.execute(
            """INSERT INTO request_log
               (request_id, created_at, client_ip, api_key_name, requested_model,
                status, is_stream, msg_count, tool_count, fingerprint,
                ingress_protocol, reasoning_effort, fast_mode)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                request_id, created, client_ip, api_key_name, requested_model,
                "pending", 1 if is_stream else 0, msg_count, tool_count,
                # log 表只存 16 字符（节省空间）；它是 affinity 表 32 字符指纹的前缀，
                # 排查时可用 `log.fingerprint || '%'` 做前缀匹配反查 cache_affinities。
                fingerprint[:16] if fingerprint else None,
                ingress_protocol or "anthropic",
                reasoning_effort,
                1 if (extract_fast_mode(request_body, ingress_protocol, request_headers) if fast_mode is None else fast_mode) else 0,
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
        _request_handles[request_id] = handle
    return handle


def update_pending(request_id: str | RequestLogHandle, **fields: Any) -> None:
    """在 pending 阶段追加一些字段（如 fingerprint / affinity_hit）。"""
    if not fields:
        return
    allowed = {
        "fingerprint", "affinity_hit", "requested_model",
        "msg_count", "tool_count", "proxy_name", "reasoning_effort", "fast_mode",
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
    handle = _request_handle(request_id)
    vals.append(handle.request_id)
    with _write_lock:
        conn = _get_conn_for_ref(handle.db)
        conn.execute(
            f"UPDATE request_log SET {', '.join(cols)} WHERE request_id=?",
            vals,
        )
        conn.commit()


def record_retry_attempt(
    request_id: str | RequestLogHandle, attempt_order: int,
    channel_key: str, channel_type: str, model: str,
    started_at: float,
    proxy_name: str | None = None,
) -> RowLogHandle:
    """Insert one outer channel attempt and return a month-bound row handle."""
    request = _request_handle(request_id)
    with _write_lock:
        conn = _get_conn_for_ref(request.db)
        cur = conn.execute(
            """INSERT INTO retry_chain
               (request_id, attempt_order, channel_key, channel_type, model, started_at, proxy_name)
               VALUES (?,?,?,?,?,?,?)""",
            (request.request_id, attempt_order, channel_key, channel_type, model, started_at, proxy_name),
        )
        conn.commit()
        return RowLogHandle(
            table="retry_chain", row_id=int(cur.lastrowid),
            request_id=request.request_id, db=request.db,
        )


def update_retry_attempt(
    attempt_id: int | RowLogHandle,
    final_round_id: str | None = None,
    connect_ms: int | None = None,
    first_byte_ms: int | None = None,
    idle_ms: int | None = None,
    attempt_elapsed_ms: int | None = None,
    request_upload_ms: int | None = None,
    response_headers_wait_ms: int | None = None,
    response_body_first_byte_wait_ms: int | None = None,
    total_ms: int | None = None,
    ended_at: float | None = None,
    outcome: str | None = None,
    error_detail: str | None = None,
    proxy_name: str | None = None,
    bytes_up: int | None = None,
    bytes_down: int | None = None,
) -> None:
    fields, vals = [], []
    if final_round_id is not None:
        fields.append("final_round_id=?"); vals.append(final_round_id)
    if connect_ms is not None:
        fields.append("connect_ms=?"); vals.append(connect_ms)
    if first_byte_ms is not None:
        fields.append("first_byte_ms=?"); vals.append(first_byte_ms)
    for name, value in (
        ("idle_ms", idle_ms),
        ("attempt_elapsed_ms", attempt_elapsed_ms),
        ("request_upload_ms", request_upload_ms),
        ("response_headers_wait_ms", response_headers_wait_ms),
        ("response_body_first_byte_wait_ms", response_body_first_byte_wait_ms),
        ("total_ms", total_ms),
    ):
        if value is not None:
            fields.append(f"{name}=?"); vals.append(int(value))
    if ended_at is not None:
        fields.append("ended_at=?"); vals.append(ended_at)
    if outcome is not None:
        fields.append("outcome=?"); vals.append(outcome)
    if error_detail is not None:
        fields.append("error_detail=?"); vals.append(error_detail)
    if proxy_name is not None:
        fields.append("proxy_name=?"); vals.append(proxy_name)
    if bytes_up is not None:
        fields.append("bytes_up=?"); vals.append(int(bytes_up or 0))
    if bytes_down is not None:
        fields.append("bytes_down=?"); vals.append(int(bytes_down or 0))
    if not fields:
        return
    handle = _row_handle(attempt_id, table="retry_chain")
    vals.append(handle.row_id)
    with _write_lock:
        conn = _get_conn_for_ref(handle.db)
        conn.execute(
            f"UPDATE retry_chain SET {', '.join(fields)} WHERE id=?",
            vals,
        )
        conn.commit()


def record_proxy_attempt(
    request_id: str | RequestLogHandle,
    retry_attempt_id: int | RowLogHandle | None,
    attempt_order: int,
    proxy_name: str,
    started_at: float,
    *,
    round_id: str | None = None,
    transport: str | None = None,
    request_mode: str | None = None,
) -> RowLogHandle:
    """Insert one real route round (including ``direct``) with a bound handle."""
    request = _request_handle(request_id)
    retry_id: int | None = None
    if retry_attempt_id is not None:
        retry = _row_handle(retry_attempt_id, table="retry_chain")
        if isinstance(retry_attempt_id, RowLogHandle) and retry.db != request.db:
            raise ValueError("retry and route round must belong to the same monthly DB")
        retry_id = retry.row_id
    with _write_lock:
        conn = _get_conn_for_ref(request.db)
        cur = conn.execute(
            """INSERT INTO proxy_chain
               (request_id, retry_attempt_id, attempt_order, round_id, transport,
                request_mode, proxy_name, started_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                request.request_id, retry_id, attempt_order, round_id, transport,
                request_mode, proxy_name, started_at,
            ),
        )
        conn.commit()
        return RowLogHandle(
            table="proxy_chain", row_id=int(cur.lastrowid),
            request_id=request.request_id, db=request.db,
        )


def update_proxy_attempt(
    proxy_attempt_id: int | RowLogHandle,
    started_at: float | None = None,
    connect_ms: int | None = None,
    first_byte_ms: int | None = None,
    idle_ms: int | None = None,
    total_ms: int | None = None,
    dns_ms: int | None = None,
    tcp_ms: int | None = None,
    proxy_tcp_ms: int | None = None,
    proxy_tunnel_ms: int | None = None,
    tls_ms: int | None = None,
    target_tls_ms: int | None = None,
    ws_handshake_ms: int | None = None,
    request_upload_ms: int | None = None,
    response_headers_wait_ms: int | None = None,
    response_body_first_byte_wait_ms: int | None = None,
    ended_at: float | None = None,
    outcome: str | None = None,
    error_detail: str | None = None,
    bytes_up: int | None = None,
    bytes_down: int | None = None,
) -> None:
    fields, vals = [], []
    if started_at is not None:
        fields.append("started_at=?"); vals.append(float(started_at))
    if connect_ms is not None:
        fields.append("connect_ms=?"); vals.append(connect_ms)
    for name, value in (
        ("first_byte_ms", first_byte_ms),
        ("idle_ms", idle_ms),
        ("total_ms", total_ms),
        ("dns_ms", dns_ms),
        ("tcp_ms", tcp_ms),
        ("proxy_tcp_ms", proxy_tcp_ms),
        ("proxy_tunnel_ms", proxy_tunnel_ms),
        ("tls_ms", tls_ms),
        ("target_tls_ms", target_tls_ms),
        ("ws_handshake_ms", ws_handshake_ms),
        ("request_upload_ms", request_upload_ms),
        ("response_headers_wait_ms", response_headers_wait_ms),
        ("response_body_first_byte_wait_ms", response_body_first_byte_wait_ms),
    ):
        if value is not None:
            fields.append(f"{name}=?"); vals.append(int(value))
    if ended_at is not None:
        fields.append("ended_at=?"); vals.append(ended_at)
    if outcome is not None:
        fields.append("outcome=?"); vals.append(outcome)
    if error_detail is not None:
        fields.append("error_detail=?"); vals.append(error_detail)
    if bytes_up is not None:
        fields.append("bytes_up=?"); vals.append(int(bytes_up or 0))
    if bytes_down is not None:
        fields.append("bytes_down=?"); vals.append(int(bytes_down or 0))
    if not fields:
        return
    handle = _row_handle(proxy_attempt_id, table="proxy_chain")
    vals.append(handle.row_id)
    with _write_lock:
        conn = _get_conn_for_ref(handle.db)
        conn.execute(
            f"UPDATE proxy_chain SET {', '.join(fields)} WHERE id=?",
            vals,
        )
        conn.commit()




def record_local_web_call(
    request_id: str | RequestLogHandle,
    round_no: int,
    tool_name: str,
    query: str | None = None,
    url: str | None = None,
    started_at: float | None = None,
) -> RowLogHandle:
    request = _request_handle(request_id)
    with _write_lock:
        conn = _get_conn_for_ref(request.db)
        cur = conn.execute(
            """INSERT INTO local_web_log
               (request_id, round_no, tool_name, query, url, started_at, status)
               VALUES (?,?,?,?,?,?,?)""",
            (
                request.request_id, int(round_no or 0), tool_name,
                query, url, float(started_at or time.time()), "running",
            ),
        )
        conn.commit()
        return RowLogHandle(
            table="local_web_log", row_id=int(cur.lastrowid),
            request_id=request.request_id, db=request.db,
        )


def finish_local_web_call(
    log_id: int | RowLogHandle,
    *,
    status: str,
    result_count: int = 0,
    content_bytes: int = 0,
    content_chars: int = 0,
    error_message: str | None = None,
    ended_at: float | None = None,
) -> None:
    handle = _row_handle(log_id, table="local_web_log")
    with _write_lock:
        conn = _get_conn_for_ref(handle.db)
        conn.execute(
            """UPDATE local_web_log SET
               ended_at=?, status=?, result_count=?, content_bytes=?, content_chars=?, error_message=?
               WHERE id=?""",
            (
                float(ended_at or time.time()), status, int(result_count or 0),
                int(content_bytes or 0), int(content_chars or 0),
                (error_message[:4000] if isinstance(error_message, str) else error_message),
                handle.row_id,
            ),
        )
        conn.commit()


def local_web_count(request_id: str | RequestLogHandle) -> int:
    handle = _request_handle(request_id)
    row = _get_conn_for_ref(handle.db).execute(
        "SELECT COUNT(*) AS n FROM local_web_log WHERE request_id=?",
        (handle.request_id,),
    ).fetchone()
    return int(row["n"] or 0) if row else 0

def finish_success(
    request_id: str | RequestLogHandle,
    final_channel_key: str,
    final_channel_type: str,
    final_model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    connect_ms: int | None = None,
    first_token_ms: int | None = None,
    idle_ms: int | None = None,
    total_ms: int | None = None,
    final_round_id: str | None = None,
    request_elapsed_ms: int | None = None,
    retry_count: int = 0,
    affinity_hit: int = 0,
    response_body: str | None = None,
    http_status: int = 200,
    upstream_protocol: str | None = None,
    upstream_transport: str | None = None,
    proxy_name: str | None = None,
    proxy_bytes_up: int | None = None,
    proxy_bytes_down: int | None = None,
    request_upload_ms: int | None = None,
    response_headers_wait_ms: int | None = None,
    response_body_first_byte_wait_ms: int | None = None,
) -> None:
    handle = _request_handle(request_id)
    with _write_lock:
        conn = _get_conn_for_ref(handle.db)
        conn.execute(
            """UPDATE request_log SET
                 status='success', finished_at=?, http_status=?,
                 final_channel_key=?, final_channel_type=?, final_model=?,
                 input_tokens=?, output_tokens=?,
                 cache_creation_tokens=?, cache_read_tokens=?,
                 connect_time_ms=?, first_token_time_ms=?, idle_time_ms=?, total_time_ms=?,
                 final_round_id=?, request_elapsed_ms=?,
                 retry_count=?, affinity_hit=?, upstream_protocol=?, upstream_transport=?, proxy_name=?,
                 proxy_bytes_up=?, proxy_bytes_down=?,
                 request_upload_ms=?, response_headers_wait_ms=?,
                 response_body_first_byte_wait_ms=?
               WHERE request_id=?""",
            (
                time.time(), http_status,
                final_channel_key, final_channel_type, final_model,
                input_tokens, output_tokens,
                cache_creation_tokens, cache_read_tokens,
                connect_ms, first_token_ms, idle_ms, total_ms,
                final_round_id, request_elapsed_ms,
                retry_count, affinity_hit, upstream_protocol, upstream_transport, proxy_name,
                int(proxy_bytes_up or 0), int(proxy_bytes_down or 0),
                request_upload_ms, response_headers_wait_ms,
                response_body_first_byte_wait_ms,
                handle.request_id,
            ),
        )
        if response_body is not None:
            conn.execute(
                "UPDATE request_detail SET response_body=? WHERE request_id=?",
                (response_body, handle.request_id),
            )
        conn.commit()
        if _request_handles.get(handle.request_id) == handle:
            _request_handles.pop(handle.request_id, None)


def finish_error(
    request_id: str | RequestLogHandle,
    error_message: str,
    retry_count: int = 0,
    final_channel_key: str | None = None,
    final_channel_type: str | None = None,
    final_model: str | None = None,
    connect_ms: int | None = None,
    first_token_ms: int | None = None,
    idle_ms: int | None = None,
    total_ms: int | None = None,
    final_round_id: str | None = None,
    request_elapsed_ms: int | None = None,
    http_status: int | None = None,
    response_body: str | None = None,
    affinity_hit: int = 0,
    upstream_protocol: str | None = None,
    upstream_transport: str | None = None,
    proxy_name: str | None = None,
    proxy_bytes_up: int | None = None,
    proxy_bytes_down: int | None = None,
    request_upload_ms: int | None = None,
    response_headers_wait_ms: int | None = None,
    response_body_first_byte_wait_ms: int | None = None,
) -> None:
    handle = _request_handle(request_id)
    with _write_lock:
        conn = _get_conn_for_ref(handle.db)
        conn.execute(
            """UPDATE request_log SET
                 status='error', finished_at=?, error_message=?, http_status=?,
                 final_channel_key=?, final_channel_type=?, final_model=?,
                 connect_time_ms=?, first_token_time_ms=?, idle_time_ms=?, total_time_ms=?,
                 final_round_id=?, request_elapsed_ms=?,
                 retry_count=?, affinity_hit=?, upstream_protocol=?, upstream_transport=?, proxy_name=?,
                 proxy_bytes_up=?, proxy_bytes_down=?,
                 request_upload_ms=?, response_headers_wait_ms=?,
                 response_body_first_byte_wait_ms=?
               WHERE request_id=?""",
            (
                time.time(), error_message, http_status,
                final_channel_key, final_channel_type, final_model,
                connect_ms, first_token_ms, idle_ms, total_ms,
                final_round_id, request_elapsed_ms,
                retry_count, affinity_hit, upstream_protocol, upstream_transport, proxy_name,
                int(proxy_bytes_up or 0), int(proxy_bytes_down or 0),
                request_upload_ms, response_headers_wait_ms,
                response_body_first_byte_wait_ms,
                handle.request_id,
            ),
        )
        if response_body is not None:
            conn.execute(
                "UPDATE request_detail SET response_body=? WHERE request_id=?",
                (response_body, handle.request_id),
            )
        conn.commit()
        if _request_handles.get(handle.request_id) == handle:
            _request_handles.pop(handle.request_id, None)


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
            conn = _open_readonly(path)
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
            raise HistoricalLogError(f"stats_lifetime failed for {name}: {exc}") from exc
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


def _extract_xai_response_from_response_body(body: str | None) -> dict | None:
    """从 xAI/Grok response body 中提取最终 response 对象。

    xAI OAuth channel 在 Parrot 内按 upstream_stream_only 处理，即使下游非流式，
    request_detail.response_body 里也常保存为 SSE event stream；最终 usage/cost 与
    实际 service_tier 位于 `response.completed` 的 `response` 对象。这里同时兼容
    普通 JSON 响应。
    """
    if not body:
        return None

    def _response_from_obj(obj: Any) -> dict | None:
        if not isinstance(obj, dict):
            return None
        resp = obj.get("response")
        if isinstance(resp, dict):
            return resp
        if isinstance(obj.get("usage"), dict) or obj.get("service_tier") is not None:
            return obj
        return None

    text = str(body).strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        resp = _response_from_obj(obj)
        if resp:
            return resp
    except Exception:
        pass

    latest: dict | None = None
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except Exception:
            continue
        resp = _response_from_obj(obj)
        if resp:
            latest = resp
    return latest


def _extract_xai_usage_from_response_body(body: str | None) -> dict | None:
    """从 xAI/Grok response body 中提取最终 usage。"""
    resp = _extract_xai_response_from_response_body(body)
    if not isinstance(resp, dict):
        return None
    usage = resp.get("usage")
    return usage if isinstance(usage, dict) else None


def xai_cost_for_channel(channel_key: str, since_ts: float = 0) -> dict:
    """聚合某 xAI/Grok OAuth channel 的 Parrot 本地调用金额与 tokens。

    金额来自 xAI 响应 usage.cost_in_usd_ticks，换算：USD=ticks/1e10。
    tokens 仍以 request_log 已归一化字段为准（input 不含 cache_read，UI 再合计）。
    """
    out: dict[str, Any] = {
        "total": 0, "success_count": 0, "error_count": 0,
        "input": 0, "output": 0, "cache_creation": 0, "cache_read": 0,
        "cost_ticks": 0, "cost_rows": 0,
        "service_tier_counts": {},
        "tps_num_tokens": 0, "tps_denom_ms": 0,
        "max_tps": None, "min_tps": None,
    }
    if _log_dir is None or not os.path.isdir(_log_dir):
        tps = _finalize_tps(out)
        out.update(tps)
        out["cost_usd"] = 0.0
        return out

    for conn, close_fn in _iter_month_conns_all(since_ts):
        try:
            rows = conn.execute(
                """SELECT
                     l.status,
                     l.is_stream,
                     l.first_token_time_ms,
                     l.total_time_ms,
                     l.input_tokens, l.output_tokens,
                     l.cache_creation_tokens, l.cache_read_tokens,
                     d.response_body
                   FROM request_log l
                   LEFT JOIN request_detail d USING(request_id)
                   WHERE l.final_channel_key=? AND l.created_at >= ?""",
                (channel_key, since_ts),
            ).fetchall()
            for r in rows:
                out["total"] += 1
                if r["status"] == "success":
                    out["success_count"] += 1
                elif r["status"] == "error":
                    out["error_count"] += 1
                out["input"] += int(r["input_tokens"] or 0)
                out["output"] += int(r["output_tokens"] or 0)
                out["cache_creation"] += int(r["cache_creation_tokens"] or 0)
                out["cache_read"] += int(r["cache_read_tokens"] or 0)
                output_tokens = int(r["output_tokens"] or 0)
                total_ms = int(r["total_time_ms"] or 0)
                first_ms = r["first_token_time_ms"]
                denom_ms = 0
                if r["status"] == "success" and output_tokens > 0:
                    if int(r["is_stream"] or 0) == 1 and first_ms is not None and total_ms > int(first_ms or 0):
                        denom_ms = total_ms - int(first_ms or 0)
                    elif int(r["is_stream"] or 0) == 0 and total_ms > 0:
                        denom_ms = total_ms
                if denom_ms > 0:
                    out["tps_num_tokens"] += output_tokens
                    out["tps_denom_ms"] += denom_ms
                    tps = output_tokens * 1000.0 / denom_ms
                    out["max_tps"] = tps if out.get("max_tps") is None else max(float(out["max_tps"]), tps)
                    out["min_tps"] = tps if out.get("min_tps") is None else min(float(out["min_tps"]), tps)
                resp = _extract_xai_response_from_response_body(r["response_body"])
                if isinstance(resp, dict):
                    tier = str(resp.get("service_tier") or "").strip().lower()
                    if tier:
                        counts = out.setdefault("service_tier_counts", {})
                        counts[tier] = int(counts.get(tier) or 0) + 1
                usage = resp.get("usage") if isinstance(resp, dict) else None
                if not isinstance(usage, dict):
                    continue
                ticks = usage.get("cost_in_usd_ticks")
                try:
                    if ticks is not None:
                        out["cost_ticks"] += int(ticks)
                        out["cost_rows"] += 1
                except (TypeError, ValueError):
                    pass
        except Exception as exc:
            raise HistoricalLogError(f"xai_cost_for_channel failed: {exc}") from exc
        finally:
            try:
                close_fn()
            except Exception:
                pass

    tps = _finalize_tps(out)
    out.update(tps)
    out["cost_usd"] = float(out["cost_ticks"] or 0) / 10_000_000_000
    return out


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
            raise HistoricalLogError(f"aggregate query failed: {exc}") from exc
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
            raise HistoricalLogError(f"channel_model_stats failed: {exc}") from exc
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


def apikey_model_stats(api_key_name: str, since_ts: float) -> list[dict]:
    """跨月按 final_model 分组聚合某 API Key 下每个模型的统计（含 TPS）。

    用于 API Key 详情页的「按模型展开」视图，口径/字段与 channel_model_stats 一致：
    每条 dict 含 final_model + tokens_for_channel 的所有字段，按 total 降序。
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
                   WHERE api_key_name=? AND created_at >= ?
                   GROUP BY COALESCE(final_model, '?')""",
                (api_key_name, since_ts),
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
            raise HistoricalLogError(f"apikey_model_stats failed: {exc}") from exc
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
            raise HistoricalLogError(f"channels_by_requested_model failed: {exc}") from exc
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
            raise HistoricalLogError(f"tps_by_channel_model failed: {exc}") from exc
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
            # Historical reads are fail-closed and read-only.  Missing nullable
            # columns are handled by projection helpers; incompatible required
            # schema raises HistoricalLogError instead of silently dropping a month.
            c = _open_readonly(path)
            yield c, c.close
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
    "connect_time_ms, first_token_time_ms, idle_time_ms, total_time_ms, "
    "final_round_id, request_elapsed_ms, "
    "request_upload_ms, response_headers_wait_ms, response_body_first_byte_wait_ms, "
    "retry_count, affinity_hit, "
    "ingress_protocol, upstream_protocol, upstream_transport, proxy_name, proxy_bytes_up, proxy_bytes_down, "
    "reasoning_effort, fast_mode, "
    "(SELECT COUNT(*) FROM local_web_log lw WHERE lw.request_id=request_log.request_id) AS local_web_count"
)


def _compatible_recent_cols(conn: sqlite3.Connection) -> str:
    """Project nullable timing columns when reading pre-migration monthly DBs."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(request_log)").fetchall()}
    sql = _RECENT_COLS
    for name in (
        "idle_time_ms", "final_round_id", "request_elapsed_ms",
        "request_upload_ms", "response_headers_wait_ms",
        "response_body_first_byte_wait_ms", "ingress_protocol",
        "upstream_protocol", "upstream_transport", "proxy_name",
        "proxy_bytes_up", "proxy_bytes_down", "reasoning_effort", "fast_mode",
    ):
        if name not in cols:
            sql = sql.replace(name, f"NULL AS {name}")
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "local_web_log" not in tables:
        sql = sql.replace(
            "(SELECT COUNT(*) FROM local_web_log lw WHERE lw.request_id=request_log.request_id) AS local_web_count",
            "0 AS local_web_count",
        )
    return sql


def _request_connect_sql(conn: sqlite3.Connection) -> str:
    """Return a read-only compatibility expression for historical bad values."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(request_log)").fetchall()}
    legacy = (
        "status='error' "
        "AND lower(COALESCE(error_message,'')) LIKE '%first byte timeout%' "
        "AND lower(COALESCE(error_message,'')) LIKE '%response header%'"
    )
    if "response_headers_wait_ms" in cols:
        legacy += " AND response_headers_wait_ms IS NULL"
    return f"CASE WHEN ({legacy}) THEN NULL ELSE connect_time_ms END"


def proxy_stats(limit: int = 20, since_ts: float | None = None) -> list[dict]:
    """Aggregate proxy usage stats across monthly log DBs.

    Includes the requested proxy metrics:
      requests/successes/failures, tokens, connect/first-byte/idle/total
      sum+sample-count pairs and averages, plus proxied request/response bytes.
    """
    lim = max(1, int(limit or 20))
    since = 0.0 if since_ts is None else float(since_ts)
    acc: dict[str, dict] = {}
    if _log_dir is None or not os.path.isdir(_log_dir):
        return []

    for conn, close_fn in _iter_month_conns_all(since):
        try:
            connect_expr = _request_connect_sql(conn)
            request_cols = {row[1] for row in conn.execute("PRAGMA table_info(request_log)").fetchall()}
            idle_sum_expr = (
                "SUM(CASE WHEN idle_time_ms IS NOT NULL THEN idle_time_ms ELSE 0 END)"
                if "idle_time_ms" in request_cols else "0"
            )
            idle_n_expr = (
                "SUM(CASE WHEN idle_time_ms IS NOT NULL THEN 1 ELSE 0 END)"
                if "idle_time_ms" in request_cols else "0"
            )
            rows = conn.execute(f"""
                SELECT proxy_name,
                       COUNT(*) AS requests,
                       SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS successes,
                       SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS failures,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(proxy_bytes_up), 0) AS bytes_up,
                       COALESCE(SUM(proxy_bytes_down), 0) AS bytes_down,
                       SUM(CASE WHEN {connect_expr} IS NOT NULL THEN {connect_expr} ELSE 0 END) AS connect_sum,
                       SUM(CASE WHEN {connect_expr} IS NOT NULL THEN 1 ELSE 0 END) AS connect_n,
                       SUM(CASE WHEN first_token_time_ms IS NOT NULL THEN first_token_time_ms ELSE 0 END) AS first_sum,
                       SUM(CASE WHEN first_token_time_ms IS NOT NULL THEN 1 ELSE 0 END) AS first_n,
                       {idle_sum_expr} AS idle_sum,
                       {idle_n_expr} AS idle_n,
                       SUM(CASE WHEN total_time_ms IS NOT NULL THEN total_time_ms ELSE 0 END) AS total_sum,
                       SUM(CASE WHEN total_time_ms IS NOT NULL THEN 1 ELSE 0 END) AS total_n
                FROM request_log
                WHERE proxy_name IS NOT NULL AND proxy_name != '' AND created_at >= ?
                GROUP BY proxy_name
            """, (since,)).fetchall()
            for r in rows:
                name = r["proxy_name"]
                if not name:
                    continue
                b = acc.setdefault(name, {
                    "proxy_name": name,
                    "requests": 0, "successes": 0, "failures": 0,
                    "input_tokens": 0, "output_tokens": 0,
                    "cache_creation_tokens": 0, "cache_read_tokens": 0,
                    "bytes_up": 0, "bytes_down": 0,
                    "connect_sum": 0, "connect_n": 0,
                    "first_sum": 0, "first_n": 0,
                    "idle_sum": 0, "idle_n": 0,
                    "total_sum": 0, "total_n": 0,
                })
                for k in ("requests", "successes", "failures", "input_tokens", "output_tokens",
                          "cache_creation_tokens", "cache_read_tokens", "bytes_up", "bytes_down",
                          "connect_sum", "connect_n", "first_sum", "first_n",
                          "idle_sum", "idle_n", "total_sum", "total_n"):
                    b[k] += int(r[k] or 0)
        except Exception as exc:
            raise HistoricalLogError(f"proxy_stats failed: {exc}") from exc
        finally:
            try:
                close_fn()
            except Exception:
                pass

    out: list[dict] = []
    for b in acc.values():
        total_tokens = int(b["input_tokens"] + b["output_tokens"] +
                           b["cache_creation_tokens"] + b["cache_read_tokens"])
        total_bytes = int(b["bytes_up"] + b["bytes_down"])
        out.append({
            "proxy_name": b["proxy_name"],
            "requests": int(b["requests"]),
            "successes": int(b["successes"]),
            "failures": int(b["failures"]),
            "input_tokens": int(b["input_tokens"]),
            "output_tokens": int(b["output_tokens"]),
            "cache_creation_tokens": int(b["cache_creation_tokens"]),
            "cache_read_tokens": int(b["cache_read_tokens"]),
            "total_tokens": total_tokens,
            "bytes_up": int(b["bytes_up"]),
            "bytes_down": int(b["bytes_down"]),
            "total_bytes": total_bytes,
            "connect_sum_ms": int(b["connect_sum"]),
            "connect_sample_count": int(b["connect_n"]),
            "first_byte_sum_ms": int(b["first_sum"]),
            "first_byte_sample_count": int(b["first_n"]),
            "idle_sum_ms": int(b["idle_sum"]),
            "idle_sample_count": int(b["idle_n"]),
            "total_sum_ms": int(b["total_sum"]),
            "total_sample_count": int(b["total_n"]),
            "avg_connect_ms": int(b["connect_sum"] / b["connect_n"]) if b["connect_n"] else 0,
            "avg_first_byte_ms": int(b["first_sum"] / b["first_n"]) if b["first_n"] else 0,
            "avg_idle_ms": int(b["idle_sum"] / b["idle_n"]) if b["idle_n"] else 0,
            "avg_total_ms": int(b["total_sum"] / b["total_n"]) if b["total_n"] else 0,
        })
    out.sort(key=lambda x: (x["requests"], x["total_bytes"]), reverse=True)
    return out[:lim]


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
    api_keys: list[str] | None = None,
    models: list[str] | None = None,
    channel_keys: list[str] | None = None,
) -> tuple[str, list]:
    conds, vals = [], []
    if channel_key:
        conds.append("final_channel_key=?"); vals.append(channel_key)
    if model:
        conds.append("(requested_model=? OR final_model=?)"); vals.extend([model, model])
    if status:
        conds.append("status=?"); vals.append(status)
    api_keys = [str(x) for x in (api_keys or []) if str(x)]
    if api_keys:
        conds.append("api_key_name IN (" + ",".join(["?"] * len(api_keys)) + ")")
        vals.extend(api_keys)
    models = [str(x) for x in (models or []) if str(x)]
    if models:
        placeholders = ",".join(["?"] * len(models))
        conds.append(f"(requested_model IN ({placeholders}) OR final_model IN ({placeholders}))")
        vals.extend(models)
        vals.extend(models)
    channel_keys = [str(x) for x in (channel_keys or []) if str(x)]
    if channel_keys:
        conds.append("final_channel_key IN (" + ",".join(["?"] * len(channel_keys)) + ")")
        vals.extend(channel_keys)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return where, vals


def _is_response_header_first_byte_timeout(outcome: object, error_detail: object) -> bool:
    """Identify the historical branch that mislabeled header wait as connect.

    Text/outcome, rather than a numeric threshold, is used so a legitimate slow
    connection is never discarded merely because it took about 90 seconds.
    """
    outcome_text = str(outcome or "").strip().lower()
    detail_text = str(error_detail or "").strip().lower()
    return (
        (outcome_text == "first_byte_timeout" or "first byte timeout" in detail_text)
        and "response header" in detail_text
    )


def compatible_connect_ms(
    connect_ms: object,
    *,
    outcome: object,
    error_detail: object,
    stage_timing_present: bool,
):
    """Return a connect sample only when it is not the identifiable legacy bug.

    Old response-header timeout rows have the precise timeout outcome/message but
    no newly-added stage timing.  New rows carry a measured header-wait (or proxy
    attempt total), so a real slow connect is retained.  No numeric cutoff is
    used.  This helper is also used at the live scorer boundary as defense in
    depth; it never rewrites a persisted row.
    """
    if not stage_timing_present and _is_response_header_first_byte_timeout(outcome, error_detail):
        return None
    return connect_ms


def _sanitize_request_timing(row: object) -> dict:
    item = dict(row)  # sqlite3.Row or an ordinary mapping
    item["connect_time_ms"] = compatible_connect_ms(
        item.get("connect_time_ms"),
        outcome=item.get("status"),
        error_detail=item.get("error_message"),
        stage_timing_present=item.get("response_headers_wait_ms") is not None,
    )
    return item


def _sanitize_retry_timing(row: object) -> dict:
    item = dict(row)
    item["connect_ms"] = compatible_connect_ms(
        item.get("connect_ms"),
        outcome=item.get("outcome"),
        error_detail=item.get("error_detail"),
        stage_timing_present=item.get("response_headers_wait_ms") is not None,
    )
    return item


def _sanitize_proxy_timing(row: object) -> dict:
    item = dict(row)
    item["connect_ms"] = compatible_connect_ms(
        item.get("connect_ms"),
        outcome=item.get("outcome"),
        error_detail=item.get("error_detail"),
        stage_timing_present=item.get("total_ms") is not None,
    )
    return item


def recent_logs(
    limit: int = 20,
    channel_key: str | None = None,
    model: str | None = None,
    status: str | None = None,
    offset: int = 0,
    api_keys: list[str] | None = None,
    models: list[str] | None = None,
    channel_keys: list[str] | None = None,
) -> list[dict]:
    where, vals = _recent_logs_where(
        channel_key, model, status,
        api_keys=api_keys, models=models, channel_keys=channel_keys,
    )
    lim = max(1, int(limit or 20))
    off = max(0, int(offset or 0))
    conn = _get_conn()
    sql = f"SELECT {_compatible_recent_cols(conn)} FROM request_log {where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
    vals.extend([lim, off])
    rows = conn.execute(sql, vals).fetchall()
    return [_sanitize_request_timing(r) for r in rows]


def recent_logs_count(
    channel_key: str | None = None,
    model: str | None = None,
    status: str | None = None,
    api_keys: list[str] | None = None,
    models: list[str] | None = None,
    channel_keys: list[str] | None = None,
) -> int:
    where, vals = _recent_logs_where(
        channel_key, model, status,
        api_keys=api_keys, models=models, channel_keys=channel_keys,
    )
    row = _get_conn().execute(f"SELECT COUNT(*) AS n FROM request_log {where}", vals).fetchone()
    return int(row["n"] or 0) if row else 0


def recent_log_values(kind: str, limit: int = 120) -> list[str]:
    """Distinct recent values for log filters (current month)."""
    lim = max(1, min(int(limit or 120), 500))
    if kind == "apikey":
        sql = """SELECT api_key_name AS v, COUNT(*) AS n FROM request_log
                 WHERE api_key_name IS NOT NULL AND api_key_name != ''
                 GROUP BY api_key_name ORDER BY n DESC, v ASC LIMIT ?"""
        vals = (lim,)
    elif kind == "model":
        sql = """SELECT v, COUNT(*) AS n FROM (
                   SELECT requested_model AS v FROM request_log
                    WHERE requested_model IS NOT NULL AND requested_model != ''
                   UNION ALL
                   SELECT final_model AS v FROM request_log
                    WHERE final_model IS NOT NULL AND final_model != ''
                 ) GROUP BY v ORDER BY n DESC, v ASC LIMIT ?"""
        vals = (lim,)
    elif kind == "channel":
        sql = """SELECT final_channel_key AS v, COUNT(*) AS n FROM request_log
                 WHERE final_channel_key IS NOT NULL AND final_channel_key != ''
                 GROUP BY final_channel_key ORDER BY n DESC, v ASC LIMIT ?"""
        vals = (lim,)
    else:
        return []
    rows = _get_conn().execute(sql, vals).fetchall()
    return [str(r["v"] or "") for r in rows if str(r["v"] or "")]


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
    # compact-rescue main rows delegate real upstream work to child request_ids
    # like '<rid>:compact:direct' / ':compact:1'.  Surface those in details so
    # the UI shows the actual compression model/channel instead of an empty
    # execution chain.
    if not chain_rows and log_row and dict(log_row).get("final_channel_key") == "compact-rescue":
        chain_rows = _get_conn().execute(
            "SELECT * FROM retry_chain WHERE request_id LIKE ? ORDER BY request_id ASC, attempt_order ASC",
            (request_id + ":%",),
        ).fetchall()
    proxy_rows = _get_conn().execute(
        "SELECT * FROM proxy_chain WHERE request_id=? ORDER BY attempt_order ASC, id ASC",
        (request_id,),
    ).fetchall()
    local_web_rows = _get_conn().execute(
        "SELECT * FROM local_web_log WHERE request_id=? ORDER BY round_no ASC, id ASC",
        (request_id,),
    ).fetchall()
    return {
        "log": _sanitize_request_timing(log_row) if log_row else None,
        "detail": dict(detail_row) if detail_row else None,
        "retry_chain": [_sanitize_retry_timing(r) for r in chain_rows],
        "proxy_chain": [_sanitize_proxy_timing(r) for r in proxy_rows],
        "local_web_log": [dict(r) for r in local_web_rows],
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
        connect_expr = _request_connect_sql(conn)
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
                 SUM(CASE WHEN status='success' AND {connect_expr} IS NOT NULL THEN {connect_expr} ELSE 0 END) AS sum_connect_ms,
                 SUM(CASE WHEN status='success' AND {connect_expr} IS NOT NULL THEN 1 ELSE 0 END) AS cnt_connect,
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
            connect_expr = _request_connect_sql(conn)
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
                     SUM(CASE WHEN status='success' AND {connect_expr} IS NOT NULL THEN {connect_expr} ELSE 0 END) AS sum_connect_ms,
                     SUM(CASE WHEN status='success' AND {connect_expr} IS NOT NULL THEN 1 ELSE 0 END) AS cnt_connect,
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
                          ingress_protocol, upstream_protocol, upstream_transport
                   FROM request_log WHERE status='error' AND created_at >= ?{_family_where_sql}
                   ORDER BY created_at DESC LIMIT 5""".format(_family_where_sql=_family_where(family)),
                (since_ts, *_family_params(family)),
            ).fetchall():
                recent_errors.append(dict(r))

            for r in conn.execute(
                f"""SELECT {_compatible_recent_cols(conn)}
                   FROM request_log WHERE created_at >= ?{_family_where(family)}
                   ORDER BY created_at DESC LIMIT 3""",
                (since_ts, *_family_params(family)),
            ).fetchall():
                recent_calls.append(_sanitize_request_timing(r))

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


# ─── reasoning effort 提取 ──────────────────────────────────────

# budget_tokens → effort 映射（与 cc_mimicry._budget_to_effort 保持一致，
# 即老大之前定的"默认提一档"策略：16384→max, 8192→xhigh, 2048→high, 其余→medium）
def _budget_to_effort_for_log(budget) -> str:
    try:
        b = int(budget)
    except (TypeError, ValueError):
        return "max"
    if b >= 16384:
        return "max"
    if b >= 8192:
        return "xhigh"
    if b >= 2048:
        return "high"
    return "medium"



# ─── Fast mode 提取 ──────────────────────────────────────────────

_FAST_MODE_BETA = "fast-mode-2026-02-01"


def _split_beta_tokens(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            out.extend(_split_beta_tokens(item))
        return out
    return [x.strip() for x in str(value).split(",") if x.strip()]


def _get_header(headers: dict | None, name: str) -> str | None:
    if not isinstance(headers, dict):
        return None
    name_l = name.lower()
    for k, v in headers.items():
        if str(k).lower() == name_l:
            return str(v)
    return None


def extract_fast_mode(
    body: dict | None,
    ingress_protocol: str = "anthropic",
    headers: dict | None = None,
) -> bool:
    """Return whether the downstream request explicitly requested Fast mode.

    Anthropic Fast mode is represented by ``speed=fast`` plus the
    ``fast-mode-2026-02-01`` beta header.  OpenAI-family latency equivalent is
    ``service_tier=priority``.  Internal Parrot hints are accepted so logging can
    run before/after bridge transforms without losing the flag.
    """
    if isinstance(body, dict):
        if body.get("_parrot_wants_fast_mode") is True:
            return True
        if str(body.get("speed") or "").strip().lower() == "fast":
            return True
        if str(body.get("service_tier") or "").strip().lower() == "priority":
            return True
        for key in (
            "betas", "anthropic_beta", "anthropic-beta", "anthropic_betas",
            "_parrot_downstream_betas",
        ):
            if _FAST_MODE_BETA in set(_split_beta_tokens(body.get(key))):
                return True

    for key in ("anthropic-beta", "anthropic_beta"):
        if _FAST_MODE_BETA in set(_split_beta_tokens(_get_header(headers, key))):
            return True

    return False


def extract_reasoning_effort(body: dict, ingress_protocol: str = "anthropic") -> str | None:
    """从请求 body 中提取归一化的思考强度。

    返回小写字符串（如 low / medium / high / xhigh / max）或 None（未开启思考）。
    不做枚举硬校验——未来新增档位（如 ultra）能自动透传。

    Anthropic 入口：
      1. output_config.effort（显式指定，最高优先）
      2. thinking.type=enabled/adaptive + budget_tokens → _budget_to_effort 推断
      3. thinking.type=disabled / 无 thinking → None
    OpenAI chat 入口：
      1. reasoning_effort（顶层字段）
    OpenAI responses / responses_ws 入口：
      1. reasoning.effort
    """
    if not isinstance(body, dict):
        return None

    if ingress_protocol == "anthropic":
        # 优先看 output_config.effort（客户端显式指定）
        oc = body.get("output_config")
        if isinstance(oc, dict):
            eff = oc.get("effort")
            if isinstance(eff, str) and eff.strip():
                return eff.strip().lower()
        # 其次看 thinking 字段
        t = body.get("thinking")
        if isinstance(t, dict):
            ttype = t.get("type")
            if ttype in ("enabled", "adaptive"):
                # 有 budget_tokens → 推断档位
                bt = t.get("budget_tokens")
                if bt is not None:
                    return _budget_to_effort_for_log(bt)
                # enabled 但没指定 budget → 默认 max
                return "max"
            # disabled / 其他 → 未开启思考
        return None

    if ingress_protocol == "chat":
        # OpenAI chat: 顶层 reasoning_effort
        eff = body.get("reasoning_effort")
        if isinstance(eff, str) and eff.strip():
            return eff.strip().lower()
        return None

    if ingress_protocol in ("responses", "responses_ws"):
        # OpenAI responses: reasoning.effort
        reasoning = body.get("reasoning")
        if isinstance(reasoning, dict):
            eff = reasoning.get("effort")
            if isinstance(eff, str) and eff.strip():
                return eff.strip().lower()
        return None

    return None
