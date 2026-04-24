"""state.db —— 运行时状态持久化，永久保留。

四张表：
  - performance_stats       渠道x模型的滑动窗口 + EMA 统计
  - channel_errors          错误阶梯冷却
  - cache_affinities        会话亲和绑定
  - oauth_quota_cache       OAuth 配额缓存（TG Bot 渲染用）

全表写操作由单一 `_write_lock` 序列化。连接采用 thread-local，WAL 模式。
"""

import os
import sqlite3
import threading
import time
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
      raw_data         TEXT
    );
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
        conn.executescript(_schema_sql())
        _migrate_affinity_prompt_cache_key_col(conn)
        _migrate_oauth_quota_cache_openai_cols(conn)
        conn.commit()
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
    with _write_lock:
        _get_conn().execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        _get_conn().commit()


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
                r1 = conn.execute(
                    "UPDATE performance_stats SET channel_key=? WHERE channel_key=?",
                    (new_ck, old_ck),
                )
                r2 = conn.execute(
                    "UPDATE channel_errors SET channel_key=? WHERE channel_key=?",
                    (new_ck, old_ck),
                )
                r3 = conn.execute(
                    "UPDATE cache_affinities SET channel_key=? WHERE channel_key=?",
                    (new_ck, old_ck),
                )
                ch_migrated += r1.rowcount + r2.rowcount + r3.rowcount
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


def checkpoint() -> None:
    conn = _get_conn()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass


def now_ms() -> int:
    return int(time.time() * 1000)


# ─── performance_stats ────────────────────────────────────────────

def perf_save(channel_key: str, model: str, stats: dict[str, Any]) -> None:
    with _write_lock:
        _get_conn().execute(
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
        _get_conn().commit()


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
    with _write_lock:
        if channel_key and model:
            _get_conn().execute(
                "DELETE FROM performance_stats WHERE channel_key=? AND model=?",
                (channel_key, model),
            )
        elif channel_key:
            _get_conn().execute(
                "DELETE FROM performance_stats WHERE channel_key=?",
                (channel_key,),
            )
        else:
            _get_conn().execute("DELETE FROM performance_stats")
        _get_conn().commit()


def perf_rename_channel(old_key: str, new_key: str) -> None:
    if old_key == new_key:
        return
    with _write_lock:
        conn = _get_conn()
        # 先删新 key 下的所有行（避免主键冲突），再把 old 的改名
        conn.execute("DELETE FROM performance_stats WHERE channel_key=?", (new_key,))
        conn.execute(
            "UPDATE performance_stats SET channel_key=? WHERE channel_key=?",
            (new_key, old_key),
        )
        conn.commit()


# ─── channel_errors ───────────────────────────────────────────────

def error_save(channel_key: str, model: str, error_count: int,
               cooldown_until: int | None, message: str | None) -> None:
    """cooldown_until: None/正数 毫秒时间戳; -1 = 永久。"""
    with _write_lock:
        _get_conn().execute(
            """INSERT OR REPLACE INTO channel_errors
               (channel_key, model, error_count, cooldown_until, last_error_message, last_error_at)
               VALUES (?,?,?,?,?,?)""",
            (channel_key, model, error_count, cooldown_until, message, now_ms()),
        )
        _get_conn().commit()


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
        if channel_key and model:
            _get_conn().execute(
                "DELETE FROM channel_errors WHERE channel_key=? AND model=?",
                (channel_key, model),
            )
        elif channel_key:
            _get_conn().execute(
                "DELETE FROM channel_errors WHERE channel_key=?",
                (channel_key,),
            )
        else:
            _get_conn().execute("DELETE FROM channel_errors")
        _get_conn().commit()


def error_rename_channel(old_key: str, new_key: str) -> None:
    if old_key == new_key:
        return
    with _write_lock:
        conn = _get_conn()
        conn.execute("DELETE FROM channel_errors WHERE channel_key=?", (new_key,))
        conn.execute(
            "UPDATE channel_errors SET channel_key=? WHERE channel_key=?",
            (new_key, old_key),
        )
        conn.commit()


# ─── cache_affinities ─────────────────────────────────────────────

def affinity_upsert(fingerprint: str, channel_key: str, model: str,
                    last_used: int | None = None,
                    prompt_cache_key: str | None = None) -> None:
    ts = last_used if last_used is not None else now_ms()
    with _write_lock:
        conn = _get_conn()
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


def affinity_touch(fingerprint: str, last_used: int | None = None) -> None:
    ts = last_used if last_used is not None else now_ms()
    with _write_lock:
        _get_conn().execute(
            "UPDATE cache_affinities SET last_used=? WHERE fingerprint=?",
            (ts, fingerprint),
        )
        _get_conn().commit()


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
    with _write_lock:
        if fingerprint:
            _get_conn().execute(
                "DELETE FROM cache_affinities WHERE fingerprint=?",
                (fingerprint,),
            )
        else:
            _get_conn().execute("DELETE FROM cache_affinities")
        _get_conn().commit()


def affinity_delete_by_channel(channel_key: str) -> None:
    with _write_lock:
        _get_conn().execute(
            "DELETE FROM cache_affinities WHERE channel_key=?",
            (channel_key,),
        )
        _get_conn().commit()


def affinity_delete_stale_channels(live_keys: Iterable[str]) -> None:
    """删除不在 live_keys 中的所有渠道对应的亲和记录。"""
    live_set = set(live_keys)
    with _write_lock:
        rows = _get_conn().execute(
            "SELECT DISTINCT channel_key FROM cache_affinities"
        ).fetchall()
        stale = [r["channel_key"] for r in rows if r["channel_key"] not in live_set]
        for k in stale:
            _get_conn().execute(
                "DELETE FROM cache_affinities WHERE channel_key=?", (k,)
            )
        _get_conn().commit()


def affinity_rename_channel(old_key: str, new_key: str) -> None:
    if old_key == new_key:
        return
    with _write_lock:
        _get_conn().execute(
            "UPDATE cache_affinities SET channel_key=? WHERE channel_key=?",
            (new_key, old_key),
        )
        _get_conn().commit()


def affinity_cleanup(ttl_ms: int) -> int:
    """清理 last_used 早于 now-ttl 的记录。返回清理条数。"""
    cutoff = now_ms() - ttl_ms
    with _write_lock:
        cur = _get_conn().execute(
            "DELETE FROM cache_affinities WHERE last_used < ?",
            (cutoff,),
        )
        _get_conn().commit()
        return cur.rowcount


# ─── client_affinities ─────────────────────────────────────────────

def client_affinity_upsert(client_key: str, channel_key: str, model: str,
                           last_used: int | None = None) -> None:
    ts = last_used if last_used is not None else now_ms()
    with _write_lock:
        conn = _get_conn()
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


def client_affinity_load_all() -> list[dict]:
    rows = _get_conn().execute("SELECT * FROM client_affinities").fetchall()
    return [dict(r) for r in rows]


def client_affinity_delete(client_key: str | None = None) -> None:
    with _write_lock:
        if client_key:
            _get_conn().execute(
                "DELETE FROM client_affinities WHERE client_key=?",
                (client_key,),
            )
        else:
            _get_conn().execute("DELETE FROM client_affinities")
        _get_conn().commit()


def client_affinity_delete_by_channel(channel_key: str) -> None:
    with _write_lock:
        _get_conn().execute(
            "DELETE FROM client_affinities WHERE channel_key=?",
            (channel_key,),
        )
        _get_conn().commit()


def client_affinity_delete_stale_channels(live_keys: Iterable[str]) -> None:
    live_set = set(live_keys)
    with _write_lock:
        rows = _get_conn().execute(
            "SELECT DISTINCT channel_key FROM client_affinities"
        ).fetchall()
        stale = [r["channel_key"] for r in rows if r["channel_key"] not in live_set]
        for k in stale:
            _get_conn().execute(
                "DELETE FROM client_affinities WHERE channel_key=?", (k,)
            )
        _get_conn().commit()


def client_affinity_rename_channel(old_key: str, new_key: str) -> None:
    if old_key == new_key:
        return
    with _write_lock:
        _get_conn().execute(
            "UPDATE client_affinities SET channel_key=? WHERE channel_key=?",
            (new_key, old_key),
        )
        _get_conn().commit()


def client_affinity_cleanup(ttl_ms: int) -> int:
    cutoff = now_ms() - ttl_ms
    with _write_lock:
        cur = _get_conn().execute(
            "DELETE FROM client_affinities WHERE last_used < ?",
            (cutoff,),
        )
        _get_conn().commit()
        return cur.rowcount


# ─── oauth_quota_cache ────────────────────────────────────────────

def quota_save(account_key: str, data: dict[str, Any],
               *, email: str | None = None) -> None:
    """按 account_key=f"{provider}:{email}" 写入 quota。

    若调用方未显式提供 email，则按 "provider:email" 拆出 email 作显示列兜底。
    """
    if email is None:
        email = account_key.split(":", 1)[1] if ":" in account_key else account_key
    with _write_lock:
        conn = _get_conn()
        values = (
            email,
            int(data.get("fetched_at", now_ms())),
            data.get("five_hour_util"),
            data.get("five_hour_reset"),
            data.get("seven_day_util"),
            data.get("seven_day_reset"),
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
            (account_key,),
        ).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO oauth_quota_cache
                   (account_key, email, fetched_at,
                    five_hour_util, five_hour_reset,
                    seven_day_util, seven_day_reset,
                    sonnet_util, sonnet_reset,
                    opus_util, opus_reset,
                    extra_used, extra_limit, extra_util,
                    raw_data)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (account_key,) + values,
            )
        else:
            conn.execute(
                """UPDATE oauth_quota_cache SET
                     email=?, fetched_at=?,
                     five_hour_util=?, five_hour_reset=?,
                     seven_day_util=?, seven_day_reset=?,
                     sonnet_util=?, sonnet_reset=?,
                     opus_util=?, opus_reset=?,
                     extra_used=?, extra_limit=?, extra_util=?,
                     raw_data=?
                   WHERE account_key=?""",
                values + (account_key,),
            )
        conn.commit()


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
            row = _get_conn().execute(
                "SELECT * FROM oauth_quota_cache WHERE email=? LIMIT 1",
                (email,),
            ).fetchone()
    else:
        row = _get_conn().execute(
            "SELECT * FROM oauth_quota_cache WHERE email=? LIMIT 1",
            (account_key_or_email,),
        ).fetchone()
    return dict(row) if row else None


def quota_load_all() -> list[dict]:
    rows = _get_conn().execute("SELECT * FROM oauth_quota_cache").fetchall()
    return [dict(r) for r in rows]


def quota_delete(account_key_or_email: str) -> None:
    with _write_lock:
        if ":" in account_key_or_email:
            _get_conn().execute(
                "DELETE FROM oauth_quota_cache WHERE account_key=?",
                (account_key_or_email,),
            )
        else:
            _get_conn().execute(
                "DELETE FROM oauth_quota_cache WHERE email=?",
                (account_key_or_email,),
            )
        _get_conn().commit()


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
    if email is None:
        email = account_key.split(":", 1)[1] if ":" in account_key else account_key
    now_ms_val = now_ms()

    with _write_lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT account_key FROM oauth_quota_cache WHERE account_key=?",
            (account_key,),
        ).fetchone()
        if row is None:
            # 不存在 → INSERT 一条，只带白名单字段，其他 NULL
            cols = ["account_key", "email", "fetched_at", "last_passive_update_at"]
            vals = [account_key, email, 0, now_ms_val]
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
            vals = list(safe.values()) + [now_ms_val, account_key]
            conn.execute(
                f"UPDATE oauth_quota_cache SET {', '.join(set_parts)} WHERE account_key=?",
                vals,
            )
        conn.commit()


def quota_save_openai_snapshot(account_key: str, snap: dict,
                               normalized: dict | None = None,
                               *, email: str | None = None) -> None:
    """OpenAI (Codex) 专用：保存从响应头解析出的限额 snapshot。

    snap: src.oauth.openai.parse_rate_limit_headers 的返回值
      {primary_used_pct / primary_reset_sec / primary_window_min /
       secondary_* / primary_over_secondary_pct / fetched_at (ms)}
    normalized: src.oauth.openai.normalize_codex_snapshot 的返回值
      {five_hour_util / five_hour_reset_sec / seven_day_util / seven_day_reset_sec}
      None 时自动 normalize（便于调用方省事）。

    复用现有 five_hour_util / seven_day_util 列：status_menu 的配额预警与
    主菜单热账户计数无需区分 provider，直接读这两个字段即可。
    """
    # 容错：调用方可能只给 snap，normalized 由本函数补
    if normalized is None:
        from .oauth import openai as _openai_provider
        normalized = _openai_provider.normalize_codex_snapshot(snap)

    now = int(time.time())
    fetched_at = int(snap.get("fetched_at") or now_ms())

    def _reset_iso(sec: int | None) -> str | None:
        if sec is None:
            return None
        ts = now + max(0, int(sec))
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))

    if email is None:
        email = account_key.split(":", 1)[1] if ":" in account_key else account_key
    passive_ts = fetched_at
    with _write_lock:
        conn = _get_conn()
        values = (
            email,
            fetched_at,
            passive_ts,
            normalized.get("five_hour_util"),
            _reset_iso(normalized.get("five_hour_reset_sec")),
            normalized.get("seven_day_util"),
            _reset_iso(normalized.get("seven_day_reset_sec")),
            snap.get("primary_used_pct"),
            snap.get("primary_reset_sec"),
            snap.get("primary_window_min"),
            snap.get("secondary_used_pct"),
            snap.get("secondary_reset_sec"),
            snap.get("secondary_window_min"),
            snap.get("primary_over_secondary_pct"),
        )
        row = conn.execute(
            "SELECT account_key FROM oauth_quota_cache WHERE account_key=?",
            (account_key,),
        ).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO oauth_quota_cache
                   (account_key, email, fetched_at, last_passive_update_at,
                    five_hour_util, five_hour_reset,
                    seven_day_util, seven_day_reset,
                    sonnet_util, sonnet_reset,
                    opus_util, opus_reset,
                    extra_used, extra_limit, extra_util, raw_data,
                    codex_primary_used_pct, codex_primary_reset_sec, codex_primary_window_min,
                    codex_secondary_used_pct, codex_secondary_reset_sec, codex_secondary_window_min,
                    codex_primary_over_secondary_pct)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    account_key,
                ) + values[:7] + (
                    None, None,        # sonnet —— OpenAI 无此维度
                    None, None,        # opus   —— 同上
                    None, None, None,  # extra_* —— Claude 专属
                    None,              # raw_data（响应头体积较小，不存）
                ) + values[7:],
            )
        else:
            conn.execute(
                """UPDATE oauth_quota_cache SET
                     email=?, fetched_at=?, last_passive_update_at=?,
                     five_hour_util=?, five_hour_reset=?,
                     seven_day_util=?, seven_day_reset=?,
                     codex_primary_used_pct=?, codex_primary_reset_sec=?, codex_primary_window_min=?,
                     codex_secondary_used_pct=?, codex_secondary_reset_sec=?, codex_secondary_window_min=?,
                     codex_primary_over_secondary_pct=?
                   WHERE account_key=?""",
                values + (account_key,),
            )
        conn.commit()
