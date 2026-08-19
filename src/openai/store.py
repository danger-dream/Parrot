"""`previous_response_id` 本地 Store。

新记录写入独立 SQLite，避免大 history 的写入和清理阻塞 ``state.db`` 中的
评分、冷却与亲和状态。升级时不在线搬迁旧大表；新库 miss 后只读查询旧
``state.db.openai_response_store``，让旧 response 链在原 TTL 内继续可用。

连接采用 thread-local，进程内写入由模块级 RLock 串行化，读取使用 WAL。
过期清理采用有界批次，每批独立提交并在批次间让出写锁。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from typing import Optional

from .. import config


# ─── 异常 ─────────────────────────────────────────────────────────


class ResponseNotFound(Exception):
    pass


class ResponseExpired(Exception):
    pass


class ResponseForbidden(Exception):
    """api_key_name 与 Store 中记录的不一致 —— 防 Key 间碰撞。"""
    pass


class ResponseIdConflict(Exception):
    """Another API key already owns this upstream response id."""
    pass


# ─── DTO ─────────────────────────────────────────────────────────


@dataclass
class StoredResponse:
    response_id: str
    parent_id: Optional[str]
    api_key_name: str
    model: str
    channel_key: Optional[str]
    created_at: float
    expires_at: float
    input_items: list[dict]
    output_items: list[dict]


# ─── 模块级状态 ───────────────────────────────────────────────────


_local = threading.local()
_write_lock = threading.RLock()
_initialized = False
_db_path: Optional[str] = None
_legacy_db_path: Optional[str] = None
_legacy_disabled_reason = ""
_DEFAULT_DB_NAME = "openai_response_store.db"
_logger = logging.getLogger(__name__)
_cleanup_warning_lock = threading.Lock()
_last_cleanup_warning_at = 0.0
_CLEANUP_WARNING_INTERVAL_SECONDS = 3600.0
_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_JOURNAL_SIZE_LIMIT_BYTES = 64 * 1024 * 1024


def _paths_same_file(left: str, right: str) -> bool:
    """Compare paths by resolution and, when possible, by inode."""
    if os.path.realpath(left) == os.path.realpath(right):
        return True
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _validate_private_path(path: str, *, directory: bool) -> os.stat_result:
    """Fail closed on symlinks, foreign owners, or unsafe access."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(info.st_mode):
        kind = "directory" if directory else "regular file"
        raise RuntimeError(f"OpenAI Store path is not a {kind}: {path}")
    if info.st_uid != os.geteuid():
        raise RuntimeError(
            f"OpenAI Store path must be owned by uid {os.geteuid()}: {path}"
        )
    mode = stat.S_IMODE(info.st_mode)
    if directory:
        # Source installs default DATA_DIR to the repository root, which is
        # commonly 0755.  That remains safe for 0600 DB files as long as
        # another uid cannot replace directory entries.  New/container data
        # directories are still created/fixed to 0700.
        if mode & 0o022:
            raise RuntimeError(
                "OpenAI Store directory must not be group/world writable "
                f"(recommended mode 0700): {path}"
            )
    elif mode & 0o077:
        raise RuntimeError(
            f"OpenAI Store path must be private (mode 0600): {path}"
        )
    return info


def _prepare_private_db_path(path: str) -> None:
    """Create a private DB inode without relying on the process umask."""
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, mode=_PRIVATE_DIR_MODE, exist_ok=True)
    _validate_private_path(parent, directory=True)

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, _PRIVATE_FILE_MODE)
    except FileExistsError:
        _validate_private_path(path, directory=False)
    else:
        os.close(fd)
        _validate_private_path(path, directory=False)


def _validate_private_store_files() -> None:
    if not _db_path:
        return
    _validate_private_path(_db_path, directory=False)
    for suffix in ("-wal", "-shm"):
        sidecar = f"{_db_path}{suffix}"
        try:
            _validate_private_path(sidecar, directory=False)
        except FileNotFoundError:
            pass


def _legacy_regular_file_exists(path: str) -> bool:
    """Return False only for a definite ENOENT legacy database.

    Permission, mount and wrong-file-type failures must remain observable so
    they cannot be rewritten as previous_response_id 404 responses.
    """
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode):
        raise sqlite3.OperationalError(
            f"legacy response store path is not a regular file: {path}"
        )
    return True


def _absolute_data_path(value: str) -> str:
    if os.path.isabs(value):
        return os.path.abspath(value)
    data_root = os.path.realpath(config.DATA_DIR)
    candidate = os.path.abspath(os.path.join(data_root, value))
    resolved = os.path.realpath(candidate)
    try:
        inside_data_dir = os.path.commonpath((data_root, resolved)) == data_root
    except ValueError:
        inside_data_dir = False
    if not inside_data_dir:
        raise RuntimeError("relative openai.store.dbPath must stay within DATA_DIR")
    # Preserve the lexical final path so _prepare_private_db_path can reject a
    # symlink inode instead of silently opening its realpath target.
    return candidate


def _resolve_legacy_db_path() -> str:
    # Preserve the existing stateDbPath resolution semantics for compatibility;
    # only the new Store path gets the stricter DATA_DIR containment check.
    value = str(config.get().get("stateDbPath", "state.db"))
    if os.path.isabs(value):
        return os.path.abspath(value)
    return os.path.abspath(os.path.join(config.DATA_DIR, value))


def _resolve_db_path() -> str:
    """Resolve ``openai.store.dbPath`` relative to DATA_DIR.

    An omitted path always gets a database distinct from ``stateDbPath``.  An
    explicitly identical path is rejected: silently accepting it would bring
    back the lock coupling this store exists to remove and would write the
    legacy database during an upgrade.
    """
    cfg = config.get()
    store_cfg = (((cfg.get("openai") or {}).get("store")) or {})
    configured = store_cfg.get("dbPath")
    path = _absolute_data_path(str(configured or _DEFAULT_DB_NAME))
    legacy = _resolve_legacy_db_path()
    if _paths_same_file(path, legacy):
        # config.get() deep-merges defaults, so the default value can be
        # present even when the user never wrote this key.
        if configured and str(configured) != _DEFAULT_DB_NAME:
            raise RuntimeError("openai.store.dbPath must differ from stateDbPath")
        # Defensive for unusual stateDbPath values matching the default.
        path = _absolute_data_path("openai_response_store.v2.db")
        if _paths_same_file(path, legacy):
            raise RuntimeError("openai.store.dbPath fallback still aliases stateDbPath")
    return path


def _close_local_connection(name: str) -> None:
    conn = getattr(_local, name, None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    setattr(_local, name, None)
    setattr(_local, f"{name}_path", None)


def _disable_legacy_fallback(reason: str) -> None:
    global _legacy_disabled_reason
    if not _legacy_disabled_reason:
        _legacy_disabled_reason = str(reason or "legacy state store unavailable")
        _logger.warning(
            "legacy state.db Response Store fallback disabled: %s",
            _legacy_disabled_reason,
        )
    _close_local_connection("legacy_conn")


def _is_corrupt_database_error(exc: BaseException) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    base_code = (int(code) & 0xFF) if isinstance(code, int) else None
    lowered = str(exc).lower()
    return base_code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB} or any(
        marker in lowered
        for marker in ("file is not a database", "database disk image is malformed")
    )


def _rollback_failed_write(conn: sqlite3.Connection) -> None:
    try:
        conn.rollback()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        if getattr(_local, "conn", None) is conn:
            _local.conn = None
            _local.conn_path = None


def _get_conn() -> sqlite3.Connection:
    if _db_path is None:
        raise RuntimeError("openai.store.init() not called")
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "conn_path", None) != _db_path:
        _close_local_connection("conn")
        conn = None
    if conn is None:
        # Check existing sidecars before SQLite can read, replace, or delete
        # them.  The owner-only-writable parent makes the post-check stable
        # against another uid racing a replacement.
        _validate_private_store_files()
        conn = sqlite3.connect(_db_path, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(f"PRAGMA journal_size_limit={_JOURNAL_SIZE_LIMIT_BYTES}")
            _validate_private_store_files()
        except BaseException:
            conn.close()
            raise
        _local.conn = conn
        _local.conn_path = _db_path
    return conn


def _get_legacy_conn() -> Optional[sqlite3.Connection]:
    """Open legacy state.db read-only, without creating a missing file."""
    if _legacy_disabled_reason:
        return None
    path = _legacy_db_path
    if not path or (_db_path and _paths_same_file(path, _db_path)):
        _close_local_connection("legacy_conn")
        return None
    if not _legacy_regular_file_exists(path):
        _close_local_connection("legacy_conn")
        return None
    conn = getattr(_local, "legacy_conn", None)
    if conn is not None and getattr(_local, "legacy_conn_path", None) != path:
        _close_local_connection("legacy_conn")
        conn = None
    if conn is None:
        try:
            uri = f"{Path(path).resolve().as_uri()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=1)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=1000")
        except sqlite3.Error as exc:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            # The legacy table is optional compatibility data. Corruption in
            # state.db must not prevent writes to the healthy independent Store.
            if _is_corrupt_database_error(exc):
                _disable_legacy_fallback(str(exc))
                return None
            if not _legacy_regular_file_exists(path):
                return None
            raise
        _local.legacy_conn = conn
        _local.legacy_conn_path = path
    return conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS openai_response_store (
  response_id   TEXT PRIMARY KEY,
  parent_id     TEXT,
  api_key_name  TEXT,
  model         TEXT,
  channel_key   TEXT,
  created_at    REAL NOT NULL,
  expires_at    REAL NOT NULL,
  input_items   TEXT NOT NULL,
  output_items  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resp_store_expires ON openai_response_store(expires_at);
CREATE INDEX IF NOT EXISTS idx_resp_store_key     ON openai_response_store(api_key_name);
"""


def init() -> None:
    global _initialized, _db_path, _legacy_db_path, _legacy_disabled_reason
    resolved = _resolve_db_path()
    legacy = _resolve_legacy_db_path()
    if _initialized and _db_path == resolved and _legacy_db_path == legacy:
        return
    if _db_path != resolved:
        _close_local_connection("conn")
    if _legacy_db_path != legacy:
        _close_local_connection("legacy_conn")
        _legacy_disabled_reason = ""
    _db_path = resolved
    _legacy_db_path = legacy
    _prepare_private_db_path(_db_path)
    conn = _get_conn()
    with _write_lock:
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
            _validate_private_store_files()
        except BaseException:
            _rollback_failed_write(conn)
            raise
    was_initialized = _initialized
    _initialized = True
    if not was_initialized:
        print(f"[openai_store] Using {_db_path} (legacy fallback: {_legacy_db_path})")


def _store_cfg() -> dict:
    return (config.get().get("openai") or {}).get("store") or {}


def is_enabled() -> bool:
    return bool(_store_cfg().get("enabled", True))


def _ttl_seconds() -> int:
    minutes = int(_store_cfg().get("ttlMinutes", 60))
    return max(60, minutes * 60)


def _cleanup_interval_seconds() -> int:
    return int(_store_cfg().get("cleanupIntervalSeconds", 300))


def _cleanup_batch_size() -> int:
    return max(1, min(int(_store_cfg().get("cleanupBatchSize", 100)), 1_000))


def _cleanup_batch_bytes() -> int:
    return max(
        1,
        min(int(_store_cfg().get("cleanupBatchBytes", 8 * 1024 * 1024)), 1024 * 1024 * 1024),
    )


def _cleanup_max_batches() -> int:
    return max(1, min(int(_store_cfg().get("cleanupMaxBatches", 100)), 1_000))


def _cleanup_time_budget_seconds() -> float:
    return max(0.01, min(float(_store_cfg().get("cleanupTimeBudgetSeconds", 10)), 60.0))


# ─── CRUD ────────────────────────────────────────────────────────


def save(response_id: str, parent_id: Optional[str], *,
         api_key_name: str, model: str, channel_key: Optional[str],
         input_items: list, output_items: list,
         ttl_seconds: Optional[int] = None) -> None:
    if not _initialized:
        return
    now = time.time()
    expires_at = now + (ttl_seconds if ttl_seconds is not None else _ttl_seconds())
    conn = _get_conn()
    with _write_lock:
        try:
            local_owner = conn.execute(
                "SELECT api_key_name FROM openai_response_store WHERE response_id=?",
                (response_id,),
            ).fetchone()
            if local_owner is None:
                legacy = _legacy_lookup(response_id)
                if (
                    legacy is not None
                    and legacy["api_key_name"] != (api_key_name or "")
                ):
                    raise ResponseIdConflict(response_id)
            cur = conn.execute(
                """INSERT INTO openai_response_store
                   (response_id, parent_id, api_key_name, model, channel_key,
                    created_at, expires_at, input_items, output_items)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(response_id) DO UPDATE SET
                     parent_id=excluded.parent_id,
                     model=excluded.model,
                     channel_key=excluded.channel_key,
                     created_at=excluded.created_at,
                     expires_at=excluded.expires_at,
                     input_items=excluded.input_items,
                     output_items=excluded.output_items
                   WHERE openai_response_store.api_key_name=excluded.api_key_name""",
                (
                    response_id, parent_id, api_key_name or "", model or "",
                    channel_key or "", now, expires_at,
                    json.dumps(input_items, ensure_ascii=False),
                    json.dumps(output_items, ensure_ascii=False),
                ),
            )
            if cur.rowcount != 1:
                raise ResponseIdConflict(response_id)
            conn.commit()
        except BaseException:
            _rollback_failed_write(conn)
            raise


def _legacy_lookup(response_id: str) -> Optional[sqlite3.Row]:
    conn = _get_legacy_conn()
    if conn is None:
        return None
    try:
        return conn.execute(
            "SELECT * FROM openai_response_store WHERE response_id=?",
            (response_id,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table: openai_response_store" in str(exc).lower():
            _disable_legacy_fallback("legacy table does not exist")
            return None
        raise
    except sqlite3.DatabaseError as exc:
        if _is_corrupt_database_error(exc):
            _disable_legacy_fallback(str(exc))
            return None
        raise


def _row_to_response(row: sqlite3.Row, response_id: str, api_key_name: str) -> StoredResponse:
    if row["api_key_name"] != (api_key_name or ""):
        raise ResponseForbidden(response_id)
    if row["expires_at"] is not None and float(row["expires_at"]) < time.time():
        raise ResponseExpired(response_id)
    try:
        input_items = json.loads(row["input_items"]) if row["input_items"] else []
        output_items = json.loads(row["output_items"]) if row["output_items"] else []
    except (TypeError, ValueError, json.JSONDecodeError):
        input_items = []
        output_items = []
    return StoredResponse(
        response_id=row["response_id"],
        parent_id=row["parent_id"] or None,
        api_key_name=row["api_key_name"] or "",
        model=row["model"] or "",
        channel_key=row["channel_key"] or None,
        created_at=float(row["created_at"]),
        expires_at=float(row["expires_at"]),
        input_items=input_items if isinstance(input_items, list) else [],
        output_items=output_items if isinstance(output_items, list) else [],
    )


def lookup(response_id: str, *, api_key_name: str) -> StoredResponse:
    if not _initialized:
        raise ResponseNotFound(response_id)
    row = _get_conn().execute(
        "SELECT * FROM openai_response_store WHERE response_id=?",
        (response_id,),
    ).fetchone()
    if row is None:
        row = _legacy_lookup(response_id)
    if row is None:
        raise ResponseNotFound(response_id)
    return _row_to_response(row, response_id, api_key_name)


def expand_history(response_id: str, *, api_key_name: str,
                   max_depth: int = 50) -> list[dict]:
    """沿 parent_id 展开为老→新 items；遇到循环或超过 max_depth 时截断。"""
    chain: list[StoredResponse] = []
    seen: set[str] = set()
    cur: Optional[str] = response_id
    while cur and cur not in seen and len(chain) < max_depth:
        seen.add(cur)
        rec = lookup(cur, api_key_name=api_key_name)
        chain.append(rec)
        cur = rec.parent_id
    chain.reverse()
    items: list[dict] = []
    for rec in chain:
        items.extend(rec.input_items)
        items.extend(rec.output_items)
    return items


def cleanup_expired(now: Optional[float] = None, *,
                    batch_size: Optional[int] = None,
                    max_batches: Optional[int] = None,
                    batch_bytes: Optional[int] = None,
                    time_budget_seconds: Optional[float] = None) -> int:
    """Delete expired rows in short byte-bounded transactions.

    Row and time limits let a backlog catch up without holding the Store write
    lock for an entire cleanup run. A single oversized row is still deleted by
    itself so it cannot permanently block progress.
    """
    if not _initialized:
        return 0
    cutoff = now if now is not None else time.time()
    size = (
        _cleanup_batch_size()
        if batch_size is None else max(1, min(int(batch_size), 1_000))
    )
    batches = (
        _cleanup_max_batches()
        if max_batches is None else max(1, min(int(max_batches), 1_000))
    )
    byte_limit = (
        _cleanup_batch_bytes()
        if batch_bytes is None else max(1, min(int(batch_bytes), 1024 * 1024 * 1024))
    )
    time_budget = (
        _cleanup_time_budget_seconds()
        if time_budget_seconds is None
        else max(0.01, min(float(time_budget_seconds), 60.0))
    )
    started = time.monotonic()
    total = 0
    conn = _get_conn()
    for _ in range(batches):
        with _write_lock:
            try:
                rows = conn.execute(
                    """SELECT response_id,
                              length(CAST(input_items AS BLOB))
                              + length(CAST(output_items AS BLOB)) AS payload_bytes
                       FROM openai_response_store
                       WHERE expires_at < ? ORDER BY expires_at LIMIT ?""",
                    (cutoff, size),
                ).fetchall()
                if not rows:
                    conn.commit()
                    break
                selected: list[tuple[str]] = []
                selected_bytes = 0
                for row in rows:
                    row_bytes = max(0, int(row["payload_bytes"] or 0))
                    if selected and selected_bytes + row_bytes > byte_limit:
                        break
                    selected.append((str(row["response_id"]),))
                    selected_bytes += row_bytes
                cur = conn.executemany(
                    "DELETE FROM openai_response_store WHERE response_id=?",
                    selected,
                )
                deleted = max(0, cur.rowcount or 0)
                conn.commit()
            except BaseException:
                _rollback_failed_write(conn)
                raise
        total += deleted
        if deleted == 0:
            break
        # Let response-save threads acquire the Store lock between batches.
        time.sleep(0)
        if time.monotonic() - started >= time_budget:
            break
    return total


async def cleanup_loop() -> None:
    """每隔 cleanupIntervalSeconds 在后台清理一轮过期记录。"""
    global _last_cleanup_warning_at
    while True:
        try:
            interval = _cleanup_interval_seconds()
        except Exception:
            interval = 300
        await asyncio.sleep(max(10, interval))
        try:
            cleared = await asyncio.to_thread(cleanup_expired)
            if cleared:
                print(f"[openai_store] cleaned {cleared} expired entries")
        except Exception as exc:
            now = time.monotonic()
            with _cleanup_warning_lock:
                should_warn = (
                    not _last_cleanup_warning_at
                    or now - _last_cleanup_warning_at >= _CLEANUP_WARNING_INTERVAL_SECONDS
                )
                if should_warn:
                    _last_cleanup_warning_at = now
            if should_warn:
                _logger.warning("openai Store cleanup failed: %s", exc, exc_info=True)


# ─── 测试辅助 ────────────────────────────────────────────────────


def _reset_for_test(*, reinitialize: bool = False) -> None:
    """仅清新库；reinitialize=True 时关闭 thread-local 连接并重置初始化状态。"""
    global _initialized, _db_path, _legacy_db_path, _legacy_disabled_reason
    global _last_cleanup_warning_at
    _last_cleanup_warning_at = 0.0
    _legacy_disabled_reason = ""
    if _initialized and not reinitialize:
        conn = _get_conn()
        with _write_lock:
            conn.execute("DELETE FROM openai_response_store")
            conn.commit()
        return
    _close_local_connection("conn")
    _close_local_connection("legacy_conn")
    if reinitialize:
        _initialized = False
        _db_path = None
        _legacy_db_path = None
