"""state.db —— 运行时状态持久化。

保存渠道性能/冷却、会话亲和、OAuth 配额、网络检查，以及 xAI 异步视频
任务的短期账号绑定。全表写操作由单一 `_write_lock` 序列化；连接采用
thread-local，WAL 模式。
"""

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable

from . import config

_local = threading.local()
# 可重入：_get_conn 在创建新连接时自身也要持锁做 CREATE TABLE，
# 而上层写函数往往先取锁再调 _get_conn；非重入锁会死锁。
_write_lock = threading.RLock()
_initialized = False
_db_path: str | None = None


def _resolve_db_path() -> str:
    cfg = config.get()
    rel = cfg.get("stateDbPath", "state.db")
    if os.path.isabs(rel):
        return rel
    # Relative paths anchor to DATA_DIR (container: /app/data; source install: BASE_DIR).
    return os.path.join(config.DATA_DIR, rel)


def _schema_sql() -> str:
    return """
    CREATE TABLE IF NOT EXISTS performance_stats (
      channel_key          TEXT NOT NULL,
      model                TEXT NOT NULL,
      total_requests       INTEGER DEFAULT 0,
      success_count        INTEGER DEFAULT 0,
      recent_requests      INTEGER DEFAULT 0,
      recent_success_count INTEGER DEFAULT 0,
      avg_connect_ms       REAL DEFAULT 0,
      avg_first_byte_ms    REAL DEFAULT 0,
      avg_total_ms         REAL DEFAULT 0,
      last_updated         INTEGER NOT NULL,
      PRIMARY KEY (channel_key, model)
    );
    CREATE INDEX IF NOT EXISTS idx_perf_updated ON performance_stats(last_updated);

    CREATE TABLE IF NOT EXISTS channel_errors (
      channel_key        TEXT NOT NULL,
      model              TEXT NOT NULL,
      error_count        INTEGER DEFAULT 0,
      cooldown_until     INTEGER,
      last_error_message TEXT,
      last_error_at      INTEGER,
      PRIMARY KEY (channel_key, model)
    );
    CREATE INDEX IF NOT EXISTS idx_cooldown ON channel_errors(cooldown_until);

    CREATE TABLE IF NOT EXISTS cache_affinities (
      fingerprint       TEXT PRIMARY KEY,
      channel_key       TEXT NOT NULL,
      model             TEXT NOT NULL,
      last_used         INTEGER NOT NULL,
      created_at        INTEGER NOT NULL,
      prompt_cache_key  TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_affinity_used ON cache_affinities(last_used);
    CREATE INDEX IF NOT EXISTS idx_affinity_channel ON cache_affinities(channel_key);

    CREATE TABLE IF NOT EXISTS client_affinities (
      client_key   TEXT PRIMARY KEY,
      channel_key  TEXT NOT NULL,
      model        TEXT NOT NULL,
      last_used    INTEGER NOT NULL,
      created_at   INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_client_aff_used ON client_affinities(last_used);
    CREATE INDEX IF NOT EXISTS idx_client_aff_channel ON client_affinities(channel_key);

    CREATE TABLE IF NOT EXISTS schema_meta (
      key   TEXT PRIMARY KEY,
      value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS oauth_quota_cache (
      account_key      TEXT PRIMARY KEY,
      email            TEXT NOT NULL,
      fetched_at       INTEGER NOT NULL,
      last_passive_update_at INTEGER,
      five_hour_util   REAL,
      five_hour_reset  TEXT,
      seven_day_util   REAL,
      seven_day_reset  TEXT,
      sonnet_util      REAL,
      sonnet_reset     TEXT,
      opus_util        REAL,
      opus_reset       TEXT,
      extra_used       REAL,
      extra_limit      REAL,
      extra_util       REAL,
      raw_data         TEXT,
      codex_window_observations TEXT
    );

    CREATE TABLE IF NOT EXISTS network_check_status (
      key          TEXT PRIMARY KEY,
      label        TEXT NOT NULL,
      category     TEXT NOT NULL,
      ok           INTEGER NOT NULL,
      detail       TEXT,
      latency_ms   INTEGER,
      checked_at   INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_network_check_category ON network_check_status(category);
    CREATE INDEX IF NOT EXISTS idx_network_check_ok ON network_check_status(ok);

    CREATE TABLE IF NOT EXISTS xai_video_jobs (
      request_id   TEXT PRIMARY KEY,
      channel_key  TEXT NOT NULL,
      api_key_name TEXT NOT NULL,
      model        TEXT NOT NULL,
      created_at   INTEGER NOT NULL,
      expires_at   INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_xai_video_jobs_expires ON xai_video_jobs(expires_at);
    CREATE INDEX IF NOT EXISTS idx_xai_video_jobs_channel ON xai_video_jobs(channel_key);
    """


def init() -> None:
    """启动时调用。确保当前连接的 schema 始终升级到最新版本。"""
    global _initialized, _db_path
    resolved = _resolve_db_path()
    if _db_path != resolved:
        old = getattr(_local, "conn", None)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
            _local.conn = None
        _db_path = resolved
    os.makedirs(os.path.dirname(_db_path) or ".", exist_ok=True)
    conn = _get_conn()
    with _write_lock:
        try:
            conn.executescript(_schema_sql())
            _migrate_affinity_prompt_cache_key_col(conn)
            _migrate_oauth_quota_cache_openai_cols(conn)
            conn.commit()
        except BaseException:
            _rollback_failed_write(conn)
            raise
    if not _initialized:
        print(f"[state_db] Using {_db_path}")
    _initialized = True


# ================================================================
# 幂等迁移：cache_affinities 增加 OpenAI prompt_cache_key
# ================================================================

def _migrate_affinity_prompt_cache_key_col(conn: sqlite3.Connection) -> None:
    """老库升级：为 OpenAI 自动 prompt_cache_key 绑定补充可空列。

    该列只被 OpenAI 协议使用；Anthropic/其他协议的亲和绑定保持 NULL，
    不改变既有调度语义。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(cache_affinities)")}
    if "prompt_cache_key" not in cols:
        conn.execute("ALTER TABLE cache_affinities ADD COLUMN prompt_cache_key TEXT")


# ================================================================
# schema_meta 读写 —— 保存线上迁移版本号 / 一次性 flag 等
# ================================================================

def schema_meta_get(key: str) -> str | None:
    row = _get_conn().execute(
        "SELECT value FROM schema_meta WHERE key=?", (key,),
    ).fetchone()
    return row["value"] if row else None


def schema_meta_set(key: str, value: str) -> None:
    def write(conn):
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
            (key, value),
        )
    _commit_write(write)


# ================================================================
# 幂等迁移：将 oauth 相关主键从 email 升级为 account_key = provider:email
# 调用方：oauth_manager.bootstrap_composite_key_migration()（启动时）
# ================================================================

COMPOSITE_KEY_VERSION = "1"
COMPOSITE_KEY_FLAG = "oauth_composite_key_version"


def composite_key_migration_done() -> bool:
    return schema_meta_get(COMPOSITE_KEY_FLAG) == COMPOSITE_KEY_VERSION


def _oauth_quota_has_account_key_col(conn: sqlite3.Connection) -> bool:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(oauth_quota_cache)")}
    return "account_key" in cols


def run_composite_key_migration(email_to_key: dict[str, str]) -> dict:
    """幂等执行一次主键迁移。

    返回统计 dict：
      {"migrated_quota_rows", "migrated_channel_rows", "skipped", "reason"}
    """
    stats = {
        "migrated_quota_rows": 0,
        "migrated_channel_rows": 0,
        "skipped": False,
        "reason": "",
    }
    conn = _get_conn()
    if composite_key_migration_done():
        stats["skipped"] = True
        stats["reason"] = "flag already set"
        return stats

    with _write_lock:
        # 之前迁移过但 flag 丢失：直接补标记
        if _oauth_quota_has_account_key_col(conn):
            schema_meta_set(COMPOSITE_KEY_FLAG, COMPOSITE_KEY_VERSION)
            stats["skipped"] = True
            stats["reason"] = "account_key column already exists; flag backfilled"
            return stats

        try:
            conn.execute("BEGIN IMMEDIATE")

            # 重建 oauth_quota_cache：保留所有旧列 + 新增 account_key PK
            old_cols = [
                r["name"]
                for r in conn.execute("PRAGMA table_info(oauth_quota_cache)")
            ]
            conn.execute("ALTER TABLE oauth_quota_cache RENAME TO oauth_quota_cache_old")
            other_cols = [c for c in old_cols if c != "email"]
            new_col_defs = ["account_key TEXT PRIMARY KEY", "email TEXT NOT NULL"]
            old_types = {
                r["name"]: r["type"]
                for r in conn.execute("PRAGMA table_info(oauth_quota_cache_old)")
            }
            for c in other_cols:
                new_col_defs.append(f"{c} {old_types.get(c, 'TEXT')}")
            conn.execute(
                f"CREATE TABLE oauth_quota_cache ({', '.join(new_col_defs)})"
            )

            # 迁数据
            moved = 0
            cursor = conn.execute("SELECT * FROM oauth_quota_cache_old")
            for row in cursor.fetchall():
                old_email = row["email"]
                ak = email_to_key.get(old_email)
                if not ak:
                    continue
                insert_cols = ["account_key", "email"] + other_cols
                placeholders = ",".join(["?"] * len(insert_cols))
                values = [ak, old_email] + [row[c] for c in other_cols]
                conn.execute(
                    f"INSERT OR REPLACE INTO oauth_quota_cache ({','.join(insert_cols)}) "
                    f"VALUES ({placeholders})",
                    values,
                )
                moved += 1
            stats["migrated_quota_rows"] = moved
            conn.execute("DROP TABLE oauth_quota_cache_old")

            # UPDATE channel_key：oauth:<email> → oauth:<provider>:<email>
            ch_migrated = 0
            for old_email, ak in email_to_key.items():
                old_ck = f"oauth:{old_email}"
                new_ck = f"oauth:{ak}"
                if old_ck == new_ck:
                    continue
                ch_migrated += _rename_channel_key_no_commit(conn, old_ck, new_ck)
            stats["migrated_channel_rows"] = ch_migrated

            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                (COMPOSITE_KEY_FLAG, COMPOSITE_KEY_VERSION),
            )
            conn.execute("COMMIT")
        except Exception as exc:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise RuntimeError(f"composite key migration failed: {exc}") from exc

    return stats

def _rename_performance_stats_channel_key_no_commit(
    conn: sqlite3.Connection,
    old_key: str,
    new_key: str,
) -> int:
    """Rename performance_stats channel_key safely.

    performance_stats has PRIMARY KEY(channel_key, model), so if both old_key
    and new_key already have the same model row, a direct UPDATE would violate
    the unique constraint. For conflicts, keep the newer row and delete old.
    """
    if old_key == new_key:
        return 0

    # 冲突行：old_key 和 new_key 下有相同 model。
    # 这里保留 last_updated 更新的那条；如果 old 更新，则用 old 覆盖 new。
    cur = conn.execute(
        """
        UPDATE performance_stats AS dst
        SET
          total_requests = src.total_requests,
          success_count = src.success_count,
          recent_requests = src.recent_requests,
          recent_success_count = src.recent_success_count,
          avg_connect_ms = src.avg_connect_ms,
          avg_first_byte_ms = src.avg_first_byte_ms,
          avg_total_ms = src.avg_total_ms,
          last_updated = src.last_updated
        FROM performance_stats AS src
        WHERE dst.channel_key = ?
          AND src.channel_key = ?
          AND dst.model = src.model
          AND src.last_updated > dst.last_updated
        """,
        (new_key, old_key),
    )
    updated_conflicts = int(cur.rowcount or 0)

    # 删除所有会冲突的 old_key 行
    cur = conn.execute(
        """
        DELETE FROM performance_stats
        WHERE channel_key = ?
          AND model IN (
            SELECT model
            FROM performance_stats
            WHERE channel_key = ?
          )
        """,
        (old_key, new_key),
    )
    deleted_conflicts = int(cur.rowcount or 0)

    # 剩下不会冲突的 old_key 行正常改名
    cur = conn.execute(
        """
        UPDATE performance_stats
        SET channel_key = ?
        WHERE channel_key = ?
        """,
        (new_key, old_key),
    )
    renamed = int(cur.rowcount or 0)

    return updated_conflicts + deleted_conflicts + renamed

def _rename_channel_key_no_commit(conn: sqlite3.Connection, old_key: str, new_key: str) -> int:
    """Rename all persisted channel mirrors inside the caller's transaction.

    Startup migrations call this directly; runtime renames call it through
    ``rename_runtime_channel_state`` and publish memory under channel_state's
    shared lifecycle lock.
    """
    if old_key == new_key:
        return 0
    count = 0
    # performance_stats 有 PRIMARY KEY(channel_key, model)，需先合并再改名。
    count += _rename_performance_stats_channel_key_no_commit(conn, old_key, new_key)
    # channel_errors 同样有 PRIMARY KEY(channel_key, model)。运行时 cooldown
    # 的既有语义是 old key 获胜；迁移路径保持一致，先删除冲突的新 key 行。
    cur = conn.execute(
        """
        DELETE FROM channel_errors
        WHERE channel_key = ?
          AND model IN (
            SELECT model FROM channel_errors WHERE channel_key = ?
          )
        """,
        (new_key, old_key),
    )
    count += int(cur.rowcount or 0)
    cur = conn.execute(
        "UPDATE channel_errors SET channel_key=? WHERE channel_key=?",
        (new_key, old_key),
    )
    count += int(cur.rowcount or 0)

    # affinity 表的主键是 fingerprint/client_key，channel_key 本身不唯一。
    for table in ("cache_affinities", "client_affinities"):
        try:
            cur = conn.execute(
                f"UPDATE {table} SET channel_key=? WHERE channel_key=?",
                (new_key, old_key),
            )
            count += int(cur.rowcount or 0)
        except sqlite3.OperationalError as exc:
            # Older DBs may not have client_affinities yet.  Other I/O/schema
            # failures must abort the migration instead of becoming a miss.
            if "no such table" not in str(exc).lower():
                raise
    return count


def _quota_rename_account_key_no_commit(conn: sqlite3.Connection,
                                        old_key: str, new_key: str, *,
                                        email: str | None = None) -> int:
    if not old_key or not new_key or old_key == new_key:
        return 0
    if email is None:
        email = new_key.split(":", 1)[1] if ":" in new_key else new_key
    old = conn.execute(
        "SELECT * FROM oauth_quota_cache WHERE account_key=?",
        (old_key,),
    ).fetchone()
    if old is None:
        return 0
    new = conn.execute(
        "SELECT * FROM oauth_quota_cache WHERE account_key=?",
        (new_key,),
    ).fetchone()
    if new is not None:
        old_ts = max(int(old["fetched_at"] or 0), int(old["last_passive_update_at"] or 0))
        new_ts = max(int(new["fetched_at"] or 0), int(new["last_passive_update_at"] or 0))
        if old_ts > new_ts:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(oauth_quota_cache)")]
            values = [
                new_key if c == "account_key"
                else email if c == "email"
                else old[c]
                for c in cols
            ]
            assignments = ",".join([f"{c}=?" for c in cols])
            conn.execute(
                f"UPDATE oauth_quota_cache SET {assignments} WHERE account_key=?",
                values + [new_key],
            )
        conn.execute("DELETE FROM oauth_quota_cache WHERE account_key=?", (old_key,))
        return 0
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(oauth_quota_cache)")]
    placeholders = ",".join(["?"] * len(cols))
    values = [
        new_key if c == "account_key"
        else email if c == "email"
        else old[c]
        for c in cols
    ]
    conn.execute(
        f"INSERT INTO oauth_quota_cache ({','.join(cols)}) VALUES ({placeholders})",
        values,
    )
    conn.execute("DELETE FROM oauth_quota_cache WHERE account_key=?", (old_key,))
    return 1


def quota_rename_account_key(old_key: str, new_key: str, *, email: str | None = None) -> int:
    """Rename oauth_quota_cache primary key without changing quota contents."""
    return _commit_write(
        lambda conn: _quota_rename_account_key_no_commit(
            conn, old_key, new_key, email=email,
        )
    )


def rename_runtime_channel_state(old_channel_key: str, new_channel_key: str, *,
                                 old_account_key: str | None = None,
                                 new_account_key: str | None = None,
                                 email: str | None = None) -> int:
    """Atomically rename every persisted mirror for one live channel."""
    if old_channel_key == new_channel_key:
        return 0

    def write(conn: sqlite3.Connection) -> int:
        changed = _rename_channel_key_no_commit(conn, old_channel_key, new_channel_key)
        if old_account_key and new_account_key and old_account_key != new_account_key:
            changed += _quota_rename_account_key_no_commit(
                conn, old_account_key, new_account_key, email=email,
            )
        return changed

    return _commit_write(write)


OPENAI_WORKSPACE_KEY_VERSION = "1"
OPENAI_WORKSPACE_KEY_FLAG = "openai_workspace_key_version"


def openai_workspace_key_migration_done() -> bool:
    return schema_meta_get(OPENAI_WORKSPACE_KEY_FLAG) == OPENAI_WORKSPACE_KEY_VERSION


def openai_workspace_key_migration_scope_done(scope_key: str) -> bool:
    """Return whether one concrete OpenAI legacy→workspace mapping was migrated."""
    return schema_meta_get(scope_key) == OPENAI_WORKSPACE_KEY_VERSION


def run_openai_workspace_key_migration(old_to_new: dict[str, dict[str, str]], *,
                                        scope_key: str | None = None) -> dict:
    """Idempotently migrate OpenAI state keys from legacy to composite identity.

    `old_to_new` maps old account_key (`openai:<email>` or historical
    `openai:<workspace_id>` / `openai:<email>:<workspace_id>:<chatgpt_account_id>`)
    to a dict containing `new` (`openai:<email>:<workspace_id>`) and display `email`.
    The caller only includes unique mappings, so ambiguous rows are deliberately
    left untouched.
    """
    stats = {
        "quota_rows": 0,
        "channel_rows": 0,
        "skipped": False,
        "reason": "",
    }
    if not old_to_new:
        stats["skipped"] = True
        stats["reason"] = "no eligible mappings"
        return stats
    flag_key = scope_key or OPENAI_WORKSPACE_KEY_FLAG
    done = (
        openai_workspace_key_migration_scope_done(flag_key)
        if scope_key else openai_workspace_key_migration_done()
    )
    if done:
        stats["skipped"] = True
        stats["reason"] = "flag already set"
        return stats

    with _write_lock:
        conn = _get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            for old_key, meta in old_to_new.items():
                new_key = str(meta.get("new") or "")
                email = str(meta.get("email") or "")
                if not old_key or not new_key or old_key == new_key:
                    continue
                old_ck = f"oauth:{old_key}"
                new_ck = f"oauth:{new_key}"
                stats["channel_rows"] += _rename_channel_key_no_commit(conn, old_ck, new_ck)

                old = conn.execute(
                    "SELECT * FROM oauth_quota_cache WHERE account_key=?",
                    (old_key,),
                ).fetchone()
                if old is not None:
                    new = conn.execute(
                        "SELECT * FROM oauth_quota_cache WHERE account_key=?",
                        (new_key,),
                    ).fetchone()
                    if new is not None:
                        old_ts = max(int(old["fetched_at"] or 0), int(old["last_passive_update_at"] or 0))
                        new_ts = max(int(new["fetched_at"] or 0), int(new["last_passive_update_at"] or 0))
                        if old_ts > new_ts:
                            cols = [r["name"] for r in conn.execute("PRAGMA table_info(oauth_quota_cache)")]
                            values = [
                                new_key if c == "account_key"
                                else email if c == "email"
                                else old[c]
                                for c in cols
                            ]
                            assignments = ",".join([f"{c}=?" for c in cols])
                            conn.execute(
                                f"UPDATE oauth_quota_cache SET {assignments} WHERE account_key=?",
                                values + [new_key],
                            )
                        conn.execute(
                            "DELETE FROM oauth_quota_cache WHERE account_key=?",
                            (old_key,),
                        )
                    else:
                        cols = [r["name"] for r in conn.execute("PRAGMA table_info(oauth_quota_cache)")]
                        placeholders = ",".join(["?"] * len(cols))
                        values = [
                            new_key if c == "account_key"
                            else email if c == "email"
                            else old[c]
                            for c in cols
                        ]
                        conn.execute(
                            f"INSERT INTO oauth_quota_cache ({','.join(cols)}) VALUES ({placeholders})",
                            values,
                        )
                        conn.execute(
                            "DELETE FROM oauth_quota_cache WHERE account_key=?",
                            (old_key,),
                        )
                        stats["quota_rows"] += 1

            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                (flag_key, OPENAI_WORKSPACE_KEY_VERSION),
            )
            conn.execute("COMMIT")
        except Exception as exc:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise RuntimeError(f"openai workspace key migration failed: {exc}") from exc
    return stats



# oauth_quota_cache 在几个里程碑里逐步加过列：
# - 统一 quota 视图：five_hour_* / seven_day_* / sonnet_* / opus_* / extra_* / raw_data
# - 2026-04-20 被动采样：last_passive_update_at
# - OpenAI Codex 快照：codex_primary_* / codex_secondary_* / codex_primary_over_secondary_pct
#
# 某些测试会手动重建老 schema（只保留 email/fetched_at 或少数字段）再继续复用同一
# 进程里的 state_db 模块；因此 init() 必须能对"已存在但缺列"的表做幂等补齐，
# 不能只依赖 CREATE TABLE IF NOT EXISTS。
_OAUTH_QUOTA_CACHE_EXTRA_COLUMNS: list[tuple[str, str]] = [
    ("five_hour_util",                   "REAL"),
    ("five_hour_reset",                  "TEXT"),
    ("seven_day_util",                   "REAL"),
    ("seven_day_reset",                  "TEXT"),
    ("thirty_day_util",                  "REAL"),
    ("thirty_day_reset",                 "TEXT"),
    ("sonnet_util",                      "REAL"),
    ("sonnet_reset",                     "TEXT"),
    ("opus_util",                        "REAL"),
    ("opus_reset",                       "TEXT"),
    ("extra_used",                       "REAL"),
    ("extra_limit",                      "REAL"),
    ("extra_util",                       "REAL"),
    ("raw_data",                         "TEXT"),
    ("last_passive_update_at",          "INTEGER"),
    ("codex_primary_used_pct",          "REAL"),
    ("codex_primary_reset_sec",         "INTEGER"),
    ("codex_primary_window_min",        "INTEGER"),
    ("codex_secondary_used_pct",        "REAL"),
    ("codex_secondary_reset_sec",       "INTEGER"),
    ("codex_secondary_window_min",      "INTEGER"),
    ("codex_primary_over_secondary_pct", "REAL"),
    ("codex_window_observations",        "TEXT"),
]


def _migrate_oauth_quota_cache_openai_cols(conn: sqlite3.Connection) -> None:
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(oauth_quota_cache)")}
    for col, col_type in _OAUTH_QUOTA_CACHE_EXTRA_COLUMNS:
        if col in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE oauth_quota_cache ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError as exc:
            # 并发启动下可能被另一进程抢跑；忽略 "duplicate column name"
            if "duplicate column name" not in str(exc).lower():
                raise


def _get_conn() -> sqlite3.Connection:
    if getattr(_local, "conn", None) is None:
        if _db_path is None:
            raise RuntimeError("state_db.init() not called")
        conn = sqlite3.connect(_db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.execute("PRAGMA journal_size_limit=1048576")
        conn.execute("PRAGMA busy_timeout=5000")
        _local.conn = conn
    return _local.conn


def _rollback_failed_write(conn: sqlite3.Connection) -> None:
    """Rollback one failed write, discarding an unusable thread connection."""
    try:
        conn.rollback()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        finally:
            if getattr(_local, "conn", None) is conn:
                _local.conn = None


def _commit_write(effect):
    """Run one state mutation with rollback-safe transaction handling."""
    with _write_lock:
        conn = _get_conn()
        try:
            result = effect(conn)
            conn.commit()
            return result
        except BaseException:
            _rollback_failed_write(conn)
            raise


@contextmanager
def optional_write_timeout(timeout_ms: int = 100):
    """Temporarily bound an auxiliary state write's SQLite lock wait.

    The global write lock keeps the connection-local PRAGMA change from
    leaking into another state mutation. Nested state writers are safe because
    ``_write_lock`` is re-entrant.
    """
    bounded = max(1, min(int(timeout_ms), 5_000))
    if _db_path is None:
        # Preserve the original initialization error at the actual write site;
        # this context manager itself must not become a new prerequisite.
        yield
        return
    with _write_lock:
        conn = _get_conn()
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        previous = int(row[0] if row is not None else 5_000)
        conn.execute(f"PRAGMA busy_timeout={bounded}")
        try:
            yield
        finally:
            if getattr(_local, "conn", None) is conn:
                try:
                    conn.execute(f"PRAGMA busy_timeout={previous}")
                except sqlite3.Error:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass
                    _local.conn = None


def checkpoint(*, mode: str = "TRUNCATE", strict: bool = False) -> tuple[int, int, int]:
    """Checkpoint the state WAL and report ``(busy, log, checkpointed)``.

    Periodic maintenance uses the default TRUNCATE mode.  Restart guards use
    ``mode="FULL", strict=True`` so a busy or incomplete checkpoint becomes a
    hard failure instead of being silently ignored.
    """
    normalized = str(mode or "TRUNCATE").strip().upper()
    if normalized not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
        raise ValueError(f"unsupported WAL checkpoint mode: {mode}")
    with _write_lock:
        row = _get_conn().execute(f"PRAGMA wal_checkpoint({normalized})").fetchone()
    if row is None or len(row) < 3:
        raise RuntimeError(f"WAL checkpoint {normalized} returned no status")
    result = (int(row[0]), int(row[1]), int(row[2]))
    busy, log_pages, checkpointed_pages = result
    if strict and (busy != 0 or checkpointed_pages != log_pages):
        raise RuntimeError(
            f"WAL checkpoint {normalized} incomplete: "
            f"busy={busy}, log={log_pages}, checkpointed={checkpointed_pages}"
        )
    return result


def online_backup(destination: str, *, verify: bool = True) -> str:
    """Create an atomic SQLite Online Backup of the live state database.

    The source connection remains online while SQLite copies a consistent
    snapshot (including committed WAL content).  The completed temporary copy
    is integrity-checked before it atomically replaces ``destination``.
    """
    if _db_path is None:
        raise RuntimeError("state_db.init() not called")
    source_path = os.path.abspath(_db_path)
    destination = os.path.abspath(destination)
    if source_path == destination:
        raise ValueError("state database backup destination equals source")
    os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
    tmp = f"{destination}.tmp-{os.getpid()}-{threading.get_ident()}"
    try:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        with _write_lock:
            target = sqlite3.connect(tmp, timeout=10)
            try:
                _get_conn().backup(target)
                target.commit()
            finally:
                target.close()

        if verify:
            check_conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True, timeout=10)
            try:
                rows = check_conn.execute("PRAGMA integrity_check").fetchall()
            finally:
                check_conn.close()
            checks = [str(row[0]) for row in rows]
            if checks != ["ok"]:
                detail = "; ".join(checks[:5]) or "no result"
                raise RuntimeError(f"state database backup integrity_check failed: {detail}")

        # Close+fsync makes the verified snapshot durable before publishing it.
        fd = os.open(tmp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, destination)
        try:
            dir_fd = os.open(os.path.dirname(destination) or ".", os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Some filesystems do not permit directory fsync; the database file
            # itself is already synced and remains a valid recovery snapshot.
            pass
        return destination
    except BaseException:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        raise


def close() -> None:
    """Close this thread's state connection after final shutdown checkpoint."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        return
    try:
        conn.close()
    finally:
        _local.conn = None


def now_ms() -> int:
    return int(time.time() * 1000)


# ─── network_check_status ───────────────────────────────────────

def network_check_save(row: dict[str, Any]) -> None:
    def write(conn):
        conn.execute(
            """INSERT OR REPLACE INTO network_check_status
               (key, label, category, ok, detail, latency_ms, checked_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                str(row.get("key") or ""),
                str(row.get("label") or row.get("key") or ""),
                str(row.get("category") or "other"),
                1 if row.get("ok") else 0,
                str(row.get("detail") or ""),
                row.get("latency_ms"),
                int(row.get("checked_at") or now_ms()),
            ),
        )
    _commit_write(write)


def network_check_load(key: str) -> dict | None:
    row = _get_conn().execute(
        "SELECT * FROM network_check_status WHERE key=?", (key,),
    ).fetchone()
    return dict(row) if row else None


def network_check_load_all() -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM network_check_status ORDER BY category, key",
    ).fetchall()
    return [dict(r) for r in rows]


def network_check_delete(key: str) -> None:
    _commit_write(
        lambda conn: conn.execute(
            "DELETE FROM network_check_status WHERE key=?", (key,),
        )
    )


def network_check_delete_stale(live_keys: set[str]) -> None:
    def write(conn):
        rows = conn.execute("SELECT key FROM network_check_status").fetchall()
        for r in rows:
            if r["key"] not in live_keys:
                conn.execute("DELETE FROM network_check_status WHERE key=?", (r["key"],))
    _commit_write(write)


# ─── xAI Imagine video job bindings ───────────────────────────────

def xai_video_job_save(
    request_id: str,
    *,
    channel_key: str,
    api_key_name: str,
    model: str,
    ttl_seconds: int,
) -> None:
    """Persist the OAuth/API-key identity needed to poll one async video job."""
    created_at = now_ms()
    expires_at = created_at + max(1, int(ttl_seconds)) * 1000
    with _write_lock:
        conn = _get_conn()
        try:
            conn.execute("DELETE FROM xai_video_jobs WHERE expires_at<=?", (created_at,))
            conn.execute(
                """INSERT OR REPLACE INTO xai_video_jobs
                   (request_id, channel_key, api_key_name, model, created_at, expires_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    request_id,
                    channel_key,
                    api_key_name,
                    model,
                    created_at,
                    expires_at,
                ),
            )
            conn.commit()
        except Exception:
            _rollback_failed_write(conn)
            raise


def xai_video_job_load(request_id: str) -> dict | None:
    """Load a live binding; expired rows are removed atomically on access."""
    now = now_ms()
    row = _get_conn().execute(
        "SELECT * FROM xai_video_jobs WHERE request_id=?", (request_id,),
    ).fetchone()
    if row is None:
        return None
    if int(row["expires_at"] or 0) > now:
        return dict(row)
    xai_video_job_delete(request_id)
    return None


def xai_video_job_delete(request_id: str | None = None) -> None:
    with _write_lock:
        conn = _get_conn()
        if request_id:
            conn.execute("DELETE FROM xai_video_jobs WHERE request_id=?", (request_id,))
        else:
            conn.execute("DELETE FROM xai_video_jobs")
        conn.commit()


def xai_video_job_cleanup(now: int | None = None) -> int:
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute(
            "DELETE FROM xai_video_jobs WHERE expires_at<=?",
            (int(now if now is not None else now_ms()),),
        )
        conn.commit()
        return int(cur.rowcount or 0)


# ─── performance_stats ────────────────────────────────────────────

def perf_save(channel_key: str, model: str, stats: dict[str, Any]) -> None:
    with _write_lock:
        conn = _get_conn()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO performance_stats
                   (channel_key, model, total_requests, success_count,
                    recent_requests, recent_success_count,
                    avg_connect_ms, avg_first_byte_ms, avg_total_ms, last_updated)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    channel_key, model,
                    int(stats.get("total_requests", 0)),
                    int(stats.get("success_count", 0)),
                    int(stats.get("recent_requests", 0)),
                    int(stats.get("recent_success_count", 0)),
                    float(stats.get("avg_connect_ms", 0.0)),
                    float(stats.get("avg_first_byte_ms", 0.0)),
                    float(stats.get("avg_total_ms", 0.0)),
                    int(stats.get("last_updated", now_ms())),
                ),
            )
            conn.commit()
        except sqlite3.Error:
            _rollback_failed_write(conn)
            raise


def perf_load(channel_key: str, model: str) -> dict | None:
    row = _get_conn().execute(
        "SELECT * FROM performance_stats WHERE channel_key=? AND model=?",
        (channel_key, model),
    ).fetchone()
    return dict(row) if row else None


def perf_load_all() -> list[dict]:
    rows = _get_conn().execute("SELECT * FROM performance_stats").fetchall()
    return [dict(r) for r in rows]


def perf_delete(channel_key: str | None = None, model: str | None = None) -> None:
    def write(conn):
        if channel_key and model:
            conn.execute(
                "DELETE FROM performance_stats WHERE channel_key=? AND model=?",
                (channel_key, model),
            )
        elif channel_key:
            conn.execute(
                "DELETE FROM performance_stats WHERE channel_key=?",
                (channel_key,),
            )
        else:
            conn.execute("DELETE FROM performance_stats")
    _commit_write(write)


def perf_rename_channel(old_key: str, new_key: str) -> None:
    if old_key == new_key:
        return
    def write(conn):
        _rename_performance_stats_channel_key_no_commit(conn, old_key, new_key)
    _commit_write(write)


# ─── channel_errors ───────────────────────────────────────────────

def error_save(channel_key: str, model: str, error_count: int,
               cooldown_until: int | None, message: str | None) -> None:
    """cooldown_until: None/正数 毫秒时间戳; -1 = 永久。"""
    with _write_lock:
        conn = _get_conn()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO channel_errors
                   (channel_key, model, error_count, cooldown_until, last_error_message, last_error_at)
                   VALUES (?,?,?,?,?,?)""",
                (channel_key, model, error_count, cooldown_until, message, now_ms()),
            )
            conn.commit()
        except Exception:
            _rollback_failed_write(conn)
            raise


def error_load(channel_key: str, model: str) -> dict | None:
    row = _get_conn().execute(
        "SELECT * FROM channel_errors WHERE channel_key=? AND model=?",
        (channel_key, model),
    ).fetchone()
    return dict(row) if row else None


def error_load_all() -> list[dict]:
    rows = _get_conn().execute("SELECT * FROM channel_errors").fetchall()
    return [dict(r) for r in rows]


def error_delete(channel_key: str | None = None, model: str | None = None) -> None:
    with _write_lock:
        conn = _get_conn()
        try:
            if channel_key and model:
                conn.execute(
                    "DELETE FROM channel_errors WHERE channel_key=? AND model=?",
                    (channel_key, model),
                )
            elif channel_key:
                conn.execute(
                    "DELETE FROM channel_errors WHERE channel_key=?",
                    (channel_key,),
                )
            else:
                conn.execute("DELETE FROM channel_errors")
            conn.commit()
        except Exception:
            _rollback_failed_write(conn)
            raise


def error_rename_channel(old_key: str, new_key: str) -> None:
    if old_key == new_key:
        return
    def write(conn):
        # Only conflicting models are replaced by old-key state.  Models that
        # exist solely on the destination identity must survive the merge.
        conn.execute(
            """
            DELETE FROM channel_errors
            WHERE channel_key=?
              AND model IN (
                SELECT model FROM channel_errors WHERE channel_key=?
              )
            """,
            (new_key, old_key),
        )
        conn.execute(
            "UPDATE channel_errors SET channel_key=? WHERE channel_key=?",
            (new_key, old_key),
        )
    _commit_write(write)


# ─── cache_affinities ─────────────────────────────────────────────

def affinity_upsert(fingerprint: str, channel_key: str, model: str,
                    last_used: int | None = None,
                    prompt_cache_key: str | None = None) -> None:
    ts = last_used if last_used is not None else now_ms()
    with _write_lock:
        conn = _get_conn()
        try:
            # 先尝试更新；若未命中则插入。prompt_cache_key=None 表示不改
            # 既有值，避免非 OpenAI 协议/老调用路径清空 OpenAI 会话缓存绑定。
            if prompt_cache_key is not None:
                cur = conn.execute(
                    """UPDATE cache_affinities
                       SET channel_key=?, model=?, last_used=?, prompt_cache_key=?
                       WHERE fingerprint=?""",
                    (channel_key, model, ts, prompt_cache_key, fingerprint),
                )
            else:
                cur = conn.execute(
                    """UPDATE cache_affinities
                       SET channel_key=?, model=?, last_used=?
                       WHERE fingerprint=?""",
                    (channel_key, model, ts, fingerprint),
                )
            if cur.rowcount == 0:
                conn.execute(
                    """INSERT INTO cache_affinities
                       (fingerprint, channel_key, model, last_used, created_at, prompt_cache_key)
                       VALUES (?,?,?,?,?,?)""",
                    (fingerprint, channel_key, model, ts, ts, prompt_cache_key),
                )
            conn.commit()
        except sqlite3.Error:
            _rollback_failed_write(conn)
            raise


def affinity_touch(fingerprint: str, last_used: int | None = None) -> None:
    ts = last_used if last_used is not None else now_ms()
    def write(conn):
        conn.execute(
            "UPDATE cache_affinities SET last_used=? WHERE fingerprint=?",
            (ts, fingerprint),
        )
    _commit_write(write)


def affinity_load(fingerprint: str) -> dict | None:
    row = _get_conn().execute(
        "SELECT * FROM cache_affinities WHERE fingerprint=?",
        (fingerprint,),
    ).fetchone()
    return dict(row) if row else None


def affinity_load_all() -> list[dict]:
    rows = _get_conn().execute("SELECT * FROM cache_affinities").fetchall()
    return [dict(r) for r in rows]


def affinity_delete(fingerprint: str | None = None) -> None:
    def write(conn):
        if fingerprint:
            conn.execute(
                "DELETE FROM cache_affinities WHERE fingerprint=?",
                (fingerprint,),
            )
        else:
            conn.execute("DELETE FROM cache_affinities")
    _commit_write(write)


def affinity_delete_by_channel(channel_key: str) -> None:
    def write(conn):
        conn.execute(
            "DELETE FROM cache_affinities WHERE channel_key=?",
            (channel_key,),
        )
    _commit_write(write)


def affinity_delete_stale_channels(live_keys: Iterable[str]) -> None:
    """删除不在 live_keys 中的所有渠道对应的亲和记录。"""
    live_set = set(live_keys)
    def write(conn):
        rows = conn.execute(
            "SELECT DISTINCT channel_key FROM cache_affinities"
        ).fetchall()
        stale = [r["channel_key"] for r in rows if r["channel_key"] not in live_set]
        for k in stale:
            conn.execute(
                "DELETE FROM cache_affinities WHERE channel_key=?", (k,)
            )
    _commit_write(write)


def affinity_rename_channel(old_key: str, new_key: str) -> None:
    if old_key == new_key:
        return
    def write(conn):
        conn.execute(
            "UPDATE cache_affinities SET channel_key=? WHERE channel_key=?",
            (new_key, old_key),
        )
    _commit_write(write)


def affinity_cleanup(ttl_ms: int, *, cutoff_ms: int | None = None) -> int:
    """清理 last_used 早于 now-ttl 的记录。返回清理条数。"""
    cutoff = cutoff_ms if cutoff_ms is not None else now_ms() - ttl_ms
    def write(conn):
        cur = conn.execute(
            "DELETE FROM cache_affinities WHERE last_used < ?",
            (cutoff,),
        )
        return cur.rowcount
    return _commit_write(write)


# ─── client_affinities ─────────────────────────────────────────────

def client_affinity_upsert(client_key: str, channel_key: str, model: str,
                           last_used: int | None = None) -> None:
    ts = last_used if last_used is not None else now_ms()
    with _write_lock:
        conn = _get_conn()
        try:
            cur = conn.execute(
                """UPDATE client_affinities
                   SET channel_key=?, model=?, last_used=?
                   WHERE client_key=?""",
                (channel_key, model, ts, client_key),
            )
            if cur.rowcount == 0:
                conn.execute(
                    """INSERT INTO client_affinities
                       (client_key, channel_key, model, last_used, created_at)
                       VALUES (?,?,?,?,?)""",
                    (client_key, channel_key, model, ts, ts),
                )
            conn.commit()
        except sqlite3.Error:
            _rollback_failed_write(conn)
            raise


def client_affinity_load_all() -> list[dict]:
    rows = _get_conn().execute("SELECT * FROM client_affinities").fetchall()
    return [dict(r) for r in rows]


def client_affinity_delete(client_key: str | None = None) -> None:
    def write(conn):
        if client_key:
            conn.execute(
                "DELETE FROM client_affinities WHERE client_key=?",
                (client_key,),
            )
        else:
            conn.execute("DELETE FROM client_affinities")
    _commit_write(write)


def client_affinity_delete_by_channel(channel_key: str) -> None:
    def write(conn):
        conn.execute(
            "DELETE FROM client_affinities WHERE channel_key=?",
            (channel_key,),
        )
    _commit_write(write)


def client_affinity_delete_stale_channels(live_keys: Iterable[str]) -> None:
    live_set = set(live_keys)
    def write(conn):
        rows = conn.execute(
            "SELECT DISTINCT channel_key FROM client_affinities"
        ).fetchall()
        stale = [r["channel_key"] for r in rows if r["channel_key"] not in live_set]
        for k in stale:
            conn.execute(
                "DELETE FROM client_affinities WHERE channel_key=?", (k,)
            )
    _commit_write(write)


def client_affinity_rename_channel(old_key: str, new_key: str) -> None:
    if old_key == new_key:
        return
    def write(conn):
        conn.execute(
            "UPDATE client_affinities SET channel_key=? WHERE channel_key=?",
            (new_key, old_key),
        )
    _commit_write(write)


def client_affinity_cleanup(ttl_ms: int, *, cutoff_ms: int | None = None) -> int:
    cutoff = cutoff_ms if cutoff_ms is not None else now_ms() - ttl_ms
    def write(conn):
        cur = conn.execute(
            "DELETE FROM client_affinities WHERE last_used < ?",
            (cutoff,),
        )
        return cur.rowcount
    return _commit_write(write)


# ─── oauth_quota_cache ────────────────────────────────────────────

def _commit_quota_write(account_key: str, effect):
    """Commit one quota-cache write against the current OAuth generation.

    A request that started before an identity rename still carries the old
    account key.  Resolve that generation while holding the shared lifecycle
    lock so it updates the renamed row instead of recreating a stale one.  The
    same lock makes the tombstone check atomic with deletion cleanup: once a
    delete starts, no late quota write can slip in after its final DELETE.
    """
    from . import channel_state

    with channel_state.mutation_lock:
        source_channel_key = f"oauth:{account_key}"
        target_channel_key = channel_state.resolve(source_channel_key)
        if (
            channel_state.is_deleted(source_channel_key)
            or channel_state.is_deleted(target_channel_key)
        ):
            return None
        prefix = "oauth:"
        target_account_key = (
            target_channel_key[len(prefix):]
            if target_channel_key.startswith(prefix)
            else account_key
        )
        with optional_write_timeout():
            return _commit_write(
                lambda conn: effect(conn, target_account_key)
            )


def _quota_display_email(account_key: str) -> str:
    """Best-effort email fallback for canonical and legacy quota keys."""
    if ":" not in account_key:
        return account_key
    provider, identity = account_key.split(":", 1)
    # OpenAI canonical keys are provider:email:workspace. Historical xAI
    # keys can also be provider:email:subject. The email is the first identity
    # component; Claude remains provider:email.
    if provider in {"openai", "xai"} and ":" in identity:
        return identity.split(":", 1)[0]
    return identity

def quota_save(account_key: str, data: dict[str, Any],
               *, email: str | None = None) -> None:
    """按 account_key=f"{provider}:{email}" 写入 quota。

    若调用方未显式提供 email，则按 "provider:email" 拆出 email 作显示列兜底。
    """
    def write(conn, target_account_key: str):
        target_email = email
        if target_email is None:
            target_email = _quota_display_email(target_account_key)
        values = (
            target_email,
            int(data.get("fetched_at", now_ms())),
            data.get("five_hour_util"),
            data.get("five_hour_reset"),
            data.get("seven_day_util"),
            data.get("seven_day_reset"),
            data.get("thirty_day_util"),
            data.get("thirty_day_reset"),
            data.get("sonnet_util"),
            data.get("sonnet_reset"),
            data.get("opus_util"),
            data.get("opus_reset"),
            data.get("extra_used"),
            data.get("extra_limit"),
            data.get("extra_util"),
            data.get("raw_data"),
        )
        row = conn.execute(
            "SELECT account_key FROM oauth_quota_cache WHERE account_key=?",
            (target_account_key,),
        ).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO oauth_quota_cache
                   (account_key, email, fetched_at,
                    five_hour_util, five_hour_reset,
                    seven_day_util, seven_day_reset,
                    thirty_day_util, thirty_day_reset,
                    sonnet_util, sonnet_reset,
                    opus_util, opus_reset,
                    extra_used, extra_limit, extra_util,
                    raw_data)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (target_account_key,) + values,
            )
        else:
            conn.execute(
                """UPDATE oauth_quota_cache SET
                     email=?, fetched_at=?,
                     five_hour_util=?, five_hour_reset=?,
                     seven_day_util=?, seven_day_reset=?,
                     thirty_day_util=?, thirty_day_reset=?,
                     sonnet_util=?, sonnet_reset=?,
                     opus_util=?, opus_reset=?,
                     extra_used=?, extra_limit=?, extra_util=?,
                     raw_data=?
                   WHERE account_key=?""",
                values + (target_account_key,),
            )
    _commit_quota_write(account_key, write)


def quota_load(account_key_or_email: str) -> dict | None:
    """按 account_key 精确匹配；若入参不含 ":" 则回退到 email 列查找（兼容）。

    若三段式 account_key 没命中（例如调用方早期写入时用了裸 email 作 PK），再兜底
    用拆出的 email 按 email 列查一次，最大程度向后兼容。
    """
    if ":" in account_key_or_email:
        row = _get_conn().execute(
            "SELECT * FROM oauth_quota_cache WHERE account_key=?",
            (account_key_or_email,),
        ).fetchone()
        if row is None:
            # 兜底：老数据可能以裸 email 作 PK 写入
            email = account_key_or_email.split(":", 1)[1]
            rows = _get_conn().execute(
                "SELECT * FROM oauth_quota_cache WHERE email=?",
                (email,),
            ).fetchall()
            row = rows[0] if len(rows) == 1 else None
    else:
        rows = _get_conn().execute(
            "SELECT * FROM oauth_quota_cache WHERE email=?",
            (account_key_or_email,),
        ).fetchall()
        row = rows[0] if len(rows) == 1 else None
    return dict(row) if row else None


def quota_load_all() -> list[dict]:
    rows = _get_conn().execute("SELECT * FROM oauth_quota_cache").fetchall()
    return [dict(r) for r in rows]


def quota_delete(account_key_or_email: str) -> None:
    def write(conn):
        if ":" in account_key_or_email:
            conn.execute(
                "DELETE FROM oauth_quota_cache WHERE account_key=?",
                (account_key_or_email,),
            )
            # Historical OpenAI keys could be stored with account_key=email after
            # early schema upgrades. Keep this as a best-effort cleanup only.
            prov, identity = account_key_or_email.split(":", 1)
            if prov == "openai":
                conn.execute(
                    "DELETE FROM oauth_quota_cache WHERE account_key=?",
                    (identity,),
                )
        else:
            conn.execute(
                "DELETE FROM oauth_quota_cache WHERE email=?",
                (account_key_or_email,),
            )
    _commit_write(write)


def quota_patch_passive(account_key: str, patch: dict,
                        *, email: str | None = None) -> None:
    """从 Anthropic 响应头采集到的 5h/7d 字段，只更新自己那段。

    与 `quota_save` 的区别：
      - quota_save 走 INSERT OR REPLACE，写全字段（主动拉 /api/oauth/usage）
      - quota_patch_passive 走 UPDATE（或 INSERT 兜底），**只动 patch 里列出的列**
        ；绝不覆盖 sonnet/opus/extra/raw_data（那些响应头没有，保留主动拉的值）

    patch 的 key 必须在白名单内：
      five_hour_util / five_hour_reset / seven_day_util / seven_day_reset
    其他 key 会被忽略（保护主动拉写入的字段）。

    若 account_key 行不存在（新账号从未主动拉过），插入一条**只含白名单字段**
    的行，其余字段全为 NULL，fetched_at=0 作为"未主动同步过"的哨兵值。
    """
    ALLOWED = {"five_hour_util", "five_hour_reset",
               "seven_day_util", "seven_day_reset"}
    safe = {k: v for k, v in patch.items() if k in ALLOWED}
    if not safe:
        return
    now_ms_val = now_ms()

    def write(conn, target_account_key: str):
        target_email = email
        if target_email is None:
            target_email = _quota_display_email(target_account_key)
        row = conn.execute(
            "SELECT account_key FROM oauth_quota_cache WHERE account_key=?",
            (target_account_key,),
        ).fetchone()
        if row is None:
            # 不存在 → INSERT 一条，只带白名单字段，其他 NULL
            cols = ["account_key", "email", "fetched_at", "last_passive_update_at"]
            vals = [target_account_key, target_email, 0, now_ms_val]
            for k, v in safe.items():
                cols.append(k)
                vals.append(v)
            placeholders = ",".join(["?"] * len(cols))
            conn.execute(
                f"INSERT INTO oauth_quota_cache ({','.join(cols)}) VALUES ({placeholders})",
                vals,
            )
        else:
            # 存在 → UPDATE 白名单字段 + last_passive_update_at
            set_parts = [f"{k}=?" for k in safe.keys()]
            set_parts.append("last_passive_update_at=?")
            vals = list(safe.values()) + [now_ms_val, target_account_key]
            conn.execute(
                f"UPDATE oauth_quota_cache SET {', '.join(set_parts)} WHERE account_key=?",
                vals,
            )
    _commit_quota_write(account_key, write)


def quota_save_openai_snapshot(account_key: str, snap: dict,
                               normalized: dict | None = None,
                               *, email: str | None = None) -> None:
    """OpenAI (Codex) 专用：保存从响应头解析出的限额 snapshot。

    snap: src.oauth.openai.parse_rate_limit_headers 的返回值
      {primary_used_pct / primary_reset_sec / primary_window_min /
       secondary_* / primary_over_secondary_pct / fetched_at (ms)}
    normalized: src.oauth.openai.normalize_codex_snapshot 的返回值
      {five_hour_util / five_hour_reset_sec / seven_day_util / seven_day_reset_sec /
       thirty_day_util / thirty_day_reset_sec}（只含本次实际观测到的语义窗口）
      None 时自动 normalize（便于调用方省事）。

    复用现有 five_hour_util / seven_day_util / thirty_day_util 列。只更新本次
    snapshot 实际包含的语义窗口，避免普通 5h/7d 响应头清空主动 WHAM 的
    30d 数据，也避免 5h/30d 响应头清空仍有效的 7d 数据。
    """
    # 容错：调用方可能只给 snap，normalized 由本函数补
    from .oauth import openai as _openai_provider
    if normalized is None:
        normalized = _openai_provider.normalize_codex_snapshot(snap)

    now = int(time.time())
    fetched_at = int(snap.get("fetched_at") or now_ms())

    def _reset_iso(sec: int | None) -> str | None:
        if sec is None:
            return None
        ts = now + max(0, int(sec))
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))

    passive_ts = fetched_at
    def write(conn, target_account_key: str):
        target_email = email
        if target_email is None:
            target_email = _quota_display_email(target_account_key)
        semantic_fields = (
            ("five_hour_util", "five_hour_util", False),
            ("five_hour_reset", "five_hour_reset_sec", True),
            ("seven_day_util", "seven_day_util", False),
            ("seven_day_reset", "seven_day_reset_sec", True),
            ("thirty_day_util", "thirty_day_util", False),
            ("thirty_day_reset", "thirty_day_reset_sec", True),
        )
        semantic_values = [
            (column, _reset_iso(normalized.get(key)) if is_reset else normalized.get(key))
            for column, key, is_reset in semantic_fields
            if key in normalized
        ]
        raw_values = (
            ("codex_primary_used_pct", snap.get("primary_used_pct")),
            ("codex_primary_reset_sec", snap.get("primary_reset_sec")),
            ("codex_primary_window_min", snap.get("primary_window_min")),
            ("codex_secondary_used_pct", snap.get("secondary_used_pct")),
            ("codex_secondary_reset_sec", snap.get("secondary_reset_sec")),
            ("codex_secondary_window_min", snap.get("secondary_window_min")),
            ("codex_primary_over_secondary_pct", snap.get("primary_over_secondary_pct")),
        )
        incoming_observations = _openai_provider.codex_snapshot_window_observations(snap)
        row = conn.execute(
            "SELECT account_key, codex_window_observations "
            "FROM oauth_quota_cache WHERE account_key=?",
            (target_account_key,),
        ).fetchone()
        existing_observations = {}
        if row is not None and row["codex_window_observations"]:
            try:
                existing_observations = json.loads(row["codex_window_observations"])
            except (TypeError, ValueError):
                existing_observations = {}
        merged_observations = _openai_provider.merge_codex_window_observations(
            existing_observations,
            incoming_observations,
        )
        observations_json = (
            json.dumps(merged_observations, ensure_ascii=False, separators=(",", ":"),
                       sort_keys=True)
            if merged_observations else None
        )
        if row is None:
            columns = ["account_key", "email", "fetched_at", "last_passive_update_at"]
            values = [target_account_key, target_email, fetched_at, passive_ts]
            for column, value in (*semantic_values, *raw_values):
                columns.append(column)
                values.append(value)
            if observations_json is not None:
                columns.append("codex_window_observations")
                values.append(observations_json)
            placeholders = ",".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO oauth_quota_cache ({','.join(columns)}) "
                f"VALUES ({placeholders})",
                values,
            )
        else:
            updates = [
                ("email", target_email),
                ("fetched_at", fetched_at),
                ("last_passive_update_at", passive_ts),
                *semantic_values,
                *raw_values,
            ]
            if incoming_observations:
                updates.append(("codex_window_observations", observations_json))
            conn.execute(
                f"UPDATE oauth_quota_cache SET "
                f"{', '.join(f'{column}=?' for column, _ in updates)} "
                "WHERE account_key=?",
                [value for _, value in updates] + [target_account_key],
            )
    _commit_quota_write(account_key, write)
