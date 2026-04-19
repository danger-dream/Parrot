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
      fingerprint  TEXT PRIMARY KEY,
      channel_key  TEXT NOT NULL,
      model        TEXT NOT NULL,
      last_used    INTEGER NOT NULL,
      created_at   INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_affinity_used ON cache_affinities(last_used);
    CREATE INDEX IF NOT EXISTS idx_affinity_channel ON cache_affinities(channel_key);

    CREATE TABLE IF NOT EXISTS oauth_quota_cache (
      email            TEXT PRIMARY KEY,
      fetched_at       INTEGER NOT NULL,
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
    """启动时调用一次。建表 + 清理过期亲和。"""
    global _initialized, _db_path
    if _initialized:
        return
    _db_path = _resolve_db_path()
    os.makedirs(os.path.dirname(_db_path) or ".", exist_ok=True)
    conn = _get_conn()
    with _write_lock:
        conn.executescript(_schema_sql())
        conn.commit()
    _initialized = True
    print(f"[state_db] Using {_db_path}")


def _get_conn() -> sqlite3.Connection:
    if getattr(_local, "conn", None) is None:
        if _db_path is None:
            raise RuntimeError("state_db.init() not called")
        conn = sqlite3.connect(_db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
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
                    last_used: int | None = None) -> None:
    ts = last_used if last_used is not None else now_ms()
    with _write_lock:
        conn = _get_conn()
        # 先尝试更新；若未命中则插入
        cur = conn.execute(
            """UPDATE cache_affinities
               SET channel_key=?, model=?, last_used=?
               WHERE fingerprint=?""",
            (channel_key, model, ts, fingerprint),
        )
        if cur.rowcount == 0:
            conn.execute(
                """INSERT INTO cache_affinities
                   (fingerprint, channel_key, model, last_used, created_at)
                   VALUES (?,?,?,?,?)""",
                (fingerprint, channel_key, model, ts, ts),
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


# ─── oauth_quota_cache ────────────────────────────────────────────

def quota_save(email: str, data: dict[str, Any]) -> None:
    with _write_lock:
        _get_conn().execute(
            """INSERT OR REPLACE INTO oauth_quota_cache
               (email, fetched_at,
                five_hour_util, five_hour_reset,
                seven_day_util, seven_day_reset,
                sonnet_util, sonnet_reset,
                opus_util, opus_reset,
                extra_used, extra_limit, extra_util,
                raw_data)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
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
            ),
        )
        _get_conn().commit()


def quota_load(email: str) -> dict | None:
    row = _get_conn().execute(
        "SELECT * FROM oauth_quota_cache WHERE email=?",
        (email,),
    ).fetchone()
    return dict(row) if row else None


def quota_load_all() -> list[dict]:
    rows = _get_conn().execute("SELECT * FROM oauth_quota_cache").fetchall()
    return [dict(r) for r in rows]


def quota_delete(email: str) -> None:
    with _write_lock:
        _get_conn().execute(
            "DELETE FROM oauth_quota_cache WHERE email=?",
            (email,),
        )
        _get_conn().commit()
