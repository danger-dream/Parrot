"""统一多媒体生成日志（保留历史模块名与数据库结构）。

数据库仍使用既有 ``image_logs.db`` 和 ``image_call_logs`` 表，避免升级时迁移、
重命名或丢失 GPT 图片历史。新字段同时承载 Grok 图片与异步视频任务；普通
文本 ``request_log`` 保持独立。``image_db`` 的旧公开函数继续兼容，新的统一
接口由 ``media_db`` 门面复用。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config

_BJT = timezone(timedelta(hours=8))
_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_db_path: str | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS image_call_logs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id      TEXT UNIQUE NOT NULL,
  created_at      REAL NOT NULL,
  finished_at     REAL,
  api_key_name    TEXT,
  action          TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'running',
  account_key     TEXT,
  account_email   TEXT,
  main_model      TEXT,
  tool_model      TEXT,
  size            TEXT,
  prompt_preview  TEXT,
  prompt_hash     TEXT,
  duration_ms     INTEGER,
  image_count     INTEGER DEFAULT 0,
  cached_images   INTEGER DEFAULT 0,
  image_bytes     INTEGER DEFAULT 0,
  cache_paths     TEXT,
  usage_json      TEXT,
  error_type      TEXT,
  error_message   TEXT,
  provider        TEXT NOT NULL DEFAULT 'openai',
  media_type      TEXT NOT NULL DEFAULT 'image',
  model           TEXT,
  upstream_request_id TEXT,
  upstream_status TEXT,
  progress        REAL,
  cost_usd_ticks  INTEGER,
  requested_count INTEGER DEFAULT 1,
  media_duration_seconds REAL,
  aspect_ratio    TEXT,
  resolution      TEXT,
  request_duration_ms INTEGER,
  last_polled_at  REAL,
  updated_at      REAL,
  expires_at      REAL,
  http_status     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_image_logs_created ON image_call_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_image_logs_status ON image_call_logs(status);
CREATE INDEX IF NOT EXISTS idx_image_logs_account ON image_call_logs(account_key);
CREATE INDEX IF NOT EXISTS idx_image_logs_action ON image_call_logs(action);

CREATE TABLE IF NOT EXISTS image_attempt_logs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  image_log_id    INTEGER NOT NULL,
  request_id      TEXT NOT NULL,
  started_at      REAL NOT NULL,
  finished_at     REAL,
  account_key     TEXT,
  account_email   TEXT,
  status          TEXT NOT NULL DEFAULT 'running',
  duration_ms     INTEGER,
  image_count     INTEGER DEFAULT 0,
  image_bytes     INTEGER DEFAULT 0,
  error_type      TEXT,
  error_message   TEXT
);
CREATE INDEX IF NOT EXISTS idx_image_attempt_log_id ON image_attempt_logs(image_log_id);
CREATE INDEX IF NOT EXISTS idx_image_attempt_account ON image_attempt_logs(account_key);
CREATE INDEX IF NOT EXISTS idx_image_attempt_created ON image_attempt_logs(started_at);
"""


_MIGRATIONS: dict[str, str] = {
    "finished_at": "ALTER TABLE image_call_logs ADD COLUMN finished_at REAL",
    "api_key_name": "ALTER TABLE image_call_logs ADD COLUMN api_key_name TEXT",
    "action": "ALTER TABLE image_call_logs ADD COLUMN action TEXT NOT NULL DEFAULT 'generate'",
    "status": "ALTER TABLE image_call_logs ADD COLUMN status TEXT NOT NULL DEFAULT 'running'",
    "account_key": "ALTER TABLE image_call_logs ADD COLUMN account_key TEXT",
    "account_email": "ALTER TABLE image_call_logs ADD COLUMN account_email TEXT",
    "main_model": "ALTER TABLE image_call_logs ADD COLUMN main_model TEXT",
    "tool_model": "ALTER TABLE image_call_logs ADD COLUMN tool_model TEXT",
    "size": "ALTER TABLE image_call_logs ADD COLUMN size TEXT",
    "prompt_preview": "ALTER TABLE image_call_logs ADD COLUMN prompt_preview TEXT",
    "prompt_hash": "ALTER TABLE image_call_logs ADD COLUMN prompt_hash TEXT",
    "duration_ms": "ALTER TABLE image_call_logs ADD COLUMN duration_ms INTEGER",
    "image_count": "ALTER TABLE image_call_logs ADD COLUMN image_count INTEGER DEFAULT 0",
    "cached_images": "ALTER TABLE image_call_logs ADD COLUMN cached_images INTEGER DEFAULT 0",
    "image_bytes": "ALTER TABLE image_call_logs ADD COLUMN image_bytes INTEGER DEFAULT 0",
    "cache_paths": "ALTER TABLE image_call_logs ADD COLUMN cache_paths TEXT",
    "usage_json": "ALTER TABLE image_call_logs ADD COLUMN usage_json TEXT",
    "error_type": "ALTER TABLE image_call_logs ADD COLUMN error_type TEXT",
    "error_message": "ALTER TABLE image_call_logs ADD COLUMN error_message TEXT",
    "provider": "ALTER TABLE image_call_logs ADD COLUMN provider TEXT NOT NULL DEFAULT 'openai'",
    "media_type": "ALTER TABLE image_call_logs ADD COLUMN media_type TEXT NOT NULL DEFAULT 'image'",
    "model": "ALTER TABLE image_call_logs ADD COLUMN model TEXT",
    "upstream_request_id": "ALTER TABLE image_call_logs ADD COLUMN upstream_request_id TEXT",
    "upstream_status": "ALTER TABLE image_call_logs ADD COLUMN upstream_status TEXT",
    "progress": "ALTER TABLE image_call_logs ADD COLUMN progress REAL",
    "cost_usd_ticks": "ALTER TABLE image_call_logs ADD COLUMN cost_usd_ticks INTEGER",
    "requested_count": "ALTER TABLE image_call_logs ADD COLUMN requested_count INTEGER DEFAULT 1",
    "media_duration_seconds": "ALTER TABLE image_call_logs ADD COLUMN media_duration_seconds REAL",
    "aspect_ratio": "ALTER TABLE image_call_logs ADD COLUMN aspect_ratio TEXT",
    "resolution": "ALTER TABLE image_call_logs ADD COLUMN resolution TEXT",
    "request_duration_ms": "ALTER TABLE image_call_logs ADD COLUMN request_duration_ms INTEGER",
    "last_polled_at": "ALTER TABLE image_call_logs ADD COLUMN last_polled_at REAL",
    "updated_at": "ALTER TABLE image_call_logs ADD COLUMN updated_at REAL",
    "expires_at": "ALTER TABLE image_call_logs ADD COLUMN expires_at REAL",
    "http_status": "ALTER TABLE image_call_logs ADD COLUMN http_status INTEGER",
}


def _resolve_db_path() -> str:
    raw = (config.get().get("images") or {}).get("dbPath") or "image_logs.db"
    raw = str(raw).strip() or "image_logs.db"
    if os.path.isabs(raw):
        return raw
    return os.path.join(config.DATA_DIR, raw)


def init() -> None:
    global _conn, _db_path
    with _lock:
        path = _resolve_db_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("PRAGMA busy_timeout=5000")
        _conn.executescript(_SCHEMA)
        _ensure_migrations(_conn)
        _conn.commit()
        _db_path = path
        cleanup_stale_running(1800)
        print(f"[image_db] Using {path}")


def _get_conn() -> sqlite3.Connection:
    if _conn is None:
        init()
    assert _conn is not None
    return _conn


def _ensure_migrations(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(image_call_logs)").fetchall()}
    for col, sql in _MIGRATIONS.items():
        if col not in cols:
            conn.execute(sql)

    # 历史行按原有语义解释为 OpenAI/GPT 图片；不删除、不复制旧记录。
    conn.execute(
        """UPDATE image_call_logs SET
             provider=COALESCE(NULLIF(provider, ''), 'openai'),
             media_type=COALESCE(NULLIF(media_type, ''), 'image'),
             model=COALESCE(NULLIF(model, ''), NULLIF(tool_model, ''), main_model),
             requested_count=COALESCE(NULLIF(requested_count, 0), 1),
             request_duration_ms=COALESCE(request_duration_ms, duration_ms),
             updated_at=COALESCE(updated_at, finished_at, created_at)
           WHERE provider IS NULL OR provider='' OR media_type IS NULL OR media_type=''
              OR model IS NULL OR model='' OR requested_count IS NULL OR requested_count=0
              OR (request_duration_ms IS NULL AND duration_ms IS NOT NULL)
              OR updated_at IS NULL"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_media_provider_type "
        "ON image_call_logs(provider, media_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_media_updated "
        "ON image_call_logs(updated_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_media_upstream_request "
        "ON image_call_logs(upstream_request_id)"
    )
    conn.commit()


def _cost_ticks_from_usage(usage: dict | None) -> int | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get("cost_in_usd_ticks")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def checkpoint() -> None:
    try:
        with _lock:
            _get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass


def migrate_account_keys(mapping: dict[str, dict[str, str]]) -> dict:
    """Rename OpenAI account keys in image call/attempt logs.

    `mapping` maps old account_key to {"new": new_key, "email": display_email}.
    """
    stats = {"call_rows": 0, "attempt_rows": 0}
    if not mapping:
        return stats
    with _lock:
        conn = _get_conn()
        for old_key, meta in mapping.items():
            new_key = str(meta.get("new") or "")
            email = str(meta.get("email") or "")
            if not old_key or not new_key or old_key == new_key:
                continue
            cur = conn.execute(
                """UPDATE image_call_logs
                   SET account_key=?, account_email=COALESCE(NULLIF(?, ''), account_email)
                   WHERE account_key=?""",
                (new_key, email, old_key),
            )
            stats["call_rows"] += int(cur.rowcount or 0)
            cur = conn.execute(
                """UPDATE image_attempt_logs
                   SET account_key=?, account_email=COALESCE(NULLIF(?, ''), account_email)
                   WHERE account_key=?""",
                (new_key, email, old_key),
            )
            stats["attempt_rows"] += int(cur.rowcount or 0)
        conn.commit()
    return stats


def start_call(
    *,
    request_id: str,
    api_key_name: str | None,
    action: str,
    main_model: str,
    tool_model: str,
    size: str | None,
    prompt_preview: str,
    prompt_hash: str,
) -> int:
    with _lock:
        conn = _get_conn()
        now = time.time()
        cur = conn.execute(
            """INSERT INTO image_call_logs
               (request_id, created_at, updated_at, api_key_name, action, status,
                main_model, tool_model, model, size, prompt_preview, prompt_hash,
                provider, media_type, requested_count)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                request_id, now, now, api_key_name, action, "running",
                main_model, tool_model, tool_model or main_model, size,
                prompt_preview, prompt_hash, "openai", "image", 1,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def finish_call(
    log_id: int,
    *,
    status: str,
    account_key: str | None = None,
    account_email: str | None = None,
    duration_ms: int | None = None,
    image_count: int = 0,
    cached_images: int = 0,
    image_bytes: int = 0,
    cache_paths: list[str] | None = None,
    usage: dict | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> None:
    with _lock:
        now = time.time()
        _get_conn().execute(
            """UPDATE image_call_logs SET
                 finished_at=?, updated_at=?, status=?,
                 account_key=COALESCE(?, account_key),
                 account_email=COALESCE(?, account_email), duration_ms=?,
                 request_duration_ms=?, image_count=?, cached_images=?,
                 image_bytes=?, cache_paths=?, usage_json=?,
                 cost_usd_ticks=COALESCE(?, cost_usd_ticks),
                 error_type=?, error_message=?
               WHERE id=?""",
            (
                now, now, status, account_key, account_email, duration_ms,
                duration_ms, int(image_count or 0), int(cached_images or 0),
                int(image_bytes or 0),
                json.dumps(cache_paths or [], ensure_ascii=False),
                json.dumps(usage, ensure_ascii=False) if isinstance(usage, dict) else None,
                _cost_ticks_from_usage(usage), error_type,
                (error_message or "")[:1000] if error_message else None,
                log_id,
            ),
        )
        _get_conn().commit()


def mark_attempt(log_id: int, *, account_key: str, account_email: str | None) -> None:
    with _lock:
        _get_conn().execute(
            "UPDATE image_call_logs SET account_key=?, account_email=?, updated_at=? WHERE id=?",
            (account_key, account_email, time.time(), log_id),
        )
        _get_conn().commit()


def start_attempt(log_id: int, *, request_id: str, account_key: str, account_email: str | None) -> int:
    """记录一次真实上游账号尝试。用于账号 Top 统计；不影响主调用总数。"""
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            """INSERT INTO image_attempt_logs
               (image_log_id, request_id, started_at, account_key, account_email, status)
               VALUES (?,?,?,?,?,?)""",
            (log_id, request_id, time.time(), account_key, account_email, "running"),
        )
        conn.commit()
        return int(cur.lastrowid)


def finish_attempt(
    attempt_id: int,
    *,
    status: str,
    duration_ms: int | None = None,
    image_count: int = 0,
    image_bytes: int = 0,
    error_type: str | None = None,
    error_message: str | None = None,
) -> None:
    with _lock:
        _get_conn().execute(
            """UPDATE image_attempt_logs SET
                 finished_at=?, status=?, duration_ms=?, image_count=?, image_bytes=?,
                 error_type=?, error_message=?
               WHERE id=?""",
            (
                time.time(), status, duration_ms, int(image_count or 0), int(image_bytes or 0),
                error_type, (error_message or "")[:1000] if error_message else None,
                attempt_id,
            ),
        )
        _get_conn().commit()


def get_log(log_id: int) -> dict | None:
    with _lock:
        row = _get_conn().execute("SELECT * FROM image_call_logs WHERE id=?", (log_id,)).fetchone()
    return dict(row) if row else None


def recent(limit: int = 10) -> list[dict]:
    with _lock:
        rows = _get_conn().execute(
            "SELECT * FROM image_call_logs WHERE media_type='image' "
            "ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def summary() -> dict:
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            """SELECT
                 COUNT(*) AS total,
                 SUM(CASE WHEN action='generate' THEN 1 ELSE 0 END) AS generate_count,
                 SUM(CASE WHEN action='edit' THEN 1 ELSE 0 END) AS edit_count,
                 SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
                 SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_count,
                 SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running_count,
                 AVG(CASE WHEN status='success' THEN duration_ms END) AS avg_duration_ms,
                 SUM(COALESCE(duration_ms,0)) AS total_duration_ms,
                 SUM(COALESCE(image_bytes,0)) AS image_bytes,
                 SUM(COALESCE(cached_images,0)) AS cached_images
               FROM image_call_logs
               WHERE media_type='image'"""
        ).fetchone()
    d = dict(row) if row else {}
    return {k: (v if v is not None else 0) for k, v in d.items()}


def account_top(limit: int = 5) -> list[dict]:
    with _lock:
        rows = _get_conn().execute(
            """SELECT
                 COALESCE(account_key, '') AS account_key,
                 COALESCE(account_email, '') AS account_email,
                 COUNT(*) AS total,
                 SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
                 SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_count,
                 SUM(COALESCE(duration_ms,0)) AS total_duration_ms,
                 AVG(CASE WHEN duration_ms IS NOT NULL THEN duration_ms END) AS avg_duration_ms,
                 SUM(COALESCE(image_bytes,0)) AS image_bytes
               FROM image_attempt_logs
               WHERE COALESCE(account_key, '') != ''
               GROUP BY account_key, account_email
               ORDER BY total DESC, success_count DESC
               LIMIT ?""",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def cleanup_stale_running(max_age_seconds: int = 1800) -> int:
    cutoff = time.time() - int(max_age_seconds)
    with _lock:
        now = time.time()
        cur = _get_conn().execute(
            """UPDATE image_call_logs
               SET status='failed', finished_at=?, updated_at=?, error_type='stale',
                   error_message='process ended before media call completed'
               WHERE status='running' AND created_at < ?""",
            (now, now, cutoff),
        )
        _get_conn().execute(
            """UPDATE image_attempt_logs
               SET status='failed', finished_at=?, error_type='stale',
                   error_message='process ended before image attempt completed'
               WHERE status='running' AND started_at < ?""",
            (now, cutoff),
        )
        _get_conn().commit()
        return int(cur.rowcount or 0)


def fmt_bjt(ts: float | None) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(float(ts), tz=_BJT).strftime("%m-%d %H:%M:%S")


def seconds_since(ts: float | None) -> int:
    if not ts:
        return 0
    return max(0, int(time.time() - float(ts)))


# ─── Unified media log API ─────────────────────────────────────────

_TERMINAL_MEDIA_STATUSES = frozenset({"success", "failed", "expired", "cancelled"})


def _prompt_metadata(prompt: str | None) -> tuple[str, str]:
    text = " ".join(str(prompt or "").split())
    preview = text[:180] + ("…" if len(text) > 180 else "")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
    return preview, digest


def start_media_call(
    *,
    request_id: str,
    api_key_name: str | None,
    provider: str,
    media_type: str,
    action: str,
    model: str,
    prompt: str | None = None,
    size: str | None = None,
    aspect_ratio: str | None = None,
    resolution: str | None = None,
    requested_count: int = 1,
    media_duration_seconds: float | None = None,
) -> int:
    """Create one logical media task without storing the full prompt or payload."""
    preview, digest = _prompt_metadata(prompt)
    now = time.time()
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            """INSERT INTO image_call_logs
               (request_id, created_at, updated_at, api_key_name,
                provider, media_type, action, status, model, size,
                aspect_ratio, resolution, requested_count,
                media_duration_seconds, prompt_preview, prompt_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(request_id), now, now, api_key_name,
                str(provider or "unknown"), str(media_type or "unknown"),
                str(action or "generate"), "running", str(model or ""), size,
                aspect_ratio, resolution, max(1, int(requested_count or 1)),
                media_duration_seconds, preview, digest,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def finish_media_call(
    log_id: int,
    *,
    status: str,
    account_key: str | None = None,
    account_email: str | None = None,
    model: str | None = None,
    upstream_request_id: str | None = None,
    upstream_status: str | None = None,
    progress: float | None = None,
    duration_ms: int | None = None,
    request_duration_ms: int | None = None,
    image_count: int | None = None,
    requested_count: int | None = None,
    media_duration_seconds: float | None = None,
    size: str | None = None,
    aspect_ratio: str | None = None,
    resolution: str | None = None,
    usage: dict | None = None,
    cached_media_count: int | None = None,
    media_bytes: int | None = None,
    cache_paths: list[str] | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
    http_status: int | None = None,
    last_polled_at: float | None = None,
    expires_at: float | None = None,
) -> None:
    """Update one logical task; terminal statuses set ``finished_at`` exactly once."""
    normalized_status = str(status or "running")
    now = time.time()
    terminal_at = now if normalized_status in _TERMINAL_MEDIA_STATUSES else None
    usage_json = (
        json.dumps(usage, ensure_ascii=False)
        if isinstance(usage, dict)
        else None
    )
    with _lock:
        conn = _get_conn()
        conn.execute(
            """UPDATE image_call_logs SET
                 status=?, updated_at=?,
                 finished_at=CASE WHEN ? IS NOT NULL THEN COALESCE(finished_at, ?) ELSE finished_at END,
                 account_key=COALESCE(?, account_key),
                 account_email=COALESCE(?, account_email),
                 model=COALESCE(NULLIF(?, ''), model),
                 upstream_request_id=COALESCE(NULLIF(?, ''), upstream_request_id),
                 upstream_status=COALESCE(NULLIF(?, ''), upstream_status),
                 progress=COALESCE(?, progress),
                 duration_ms=COALESCE(?, duration_ms),
                 request_duration_ms=COALESCE(?, request_duration_ms),
                 image_count=COALESCE(?, image_count),
                 requested_count=COALESCE(?, requested_count),
                 media_duration_seconds=COALESCE(?, media_duration_seconds),
                 size=COALESCE(NULLIF(?, ''), size),
                 aspect_ratio=COALESCE(NULLIF(?, ''), aspect_ratio),
                 resolution=COALESCE(NULLIF(?, ''), resolution),
                 usage_json=COALESCE(?, usage_json),
                 cost_usd_ticks=COALESCE(?, cost_usd_ticks),
                 cached_images=COALESCE(?, cached_images),
                 image_bytes=COALESCE(?, image_bytes),
                 cache_paths=COALESCE(?, cache_paths),
                 error_type=?, error_message=?,
                 http_status=COALESCE(?, http_status),
                 last_polled_at=COALESCE(?, last_polled_at),
                 expires_at=COALESCE(?, expires_at)
               WHERE id=?""",
            (
                normalized_status, now, terminal_at, terminal_at,
                account_key, account_email, model, upstream_request_id,
                upstream_status, progress, duration_ms, request_duration_ms,
                image_count, requested_count, media_duration_seconds, size,
                aspect_ratio, resolution, usage_json,
                _cost_ticks_from_usage(usage), cached_media_count, media_bytes,
                json.dumps(cache_paths, ensure_ascii=False) if cache_paths is not None else None,
                error_type, (error_message or "")[:1000] if error_message else None,
                http_status, last_polled_at, expires_at, int(log_id),
            ),
        )
        conn.commit()


def media_log_for_upstream(upstream_request_id: str) -> dict | None:
    with _lock:
        row = _get_conn().execute(
            "SELECT * FROM image_call_logs WHERE upstream_request_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (str(upstream_request_id),),
        ).fetchone()
    return dict(row) if row else None


def update_media_job(
    upstream_request_id: str,
    *,
    status: str | None = None,
    **fields: Any,
) -> bool:
    """Update an async video task in place; polling never inserts another row."""
    row = media_log_for_upstream(upstream_request_id)
    if row is None:
        return False
    normalized_status = str(status or row.get("status") or "pending")
    if (
        normalized_status in _TERMINAL_MEDIA_STATUSES
        and fields.get("duration_ms") is None
        and row.get("duration_ms") is None
    ):
        fields["duration_ms"] = max(
            0,
            int((time.time() - float(row.get("created_at") or time.time())) * 1000),
        )
    finish_media_call(int(row["id"]), status=normalized_status, **fields)
    return True


def cleanup_expired_media(now: float | None = None) -> int:
    ts = float(now if now is not None else time.time())
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            """UPDATE image_call_logs SET
                 status='expired', upstream_status='expired',
                 finished_at=COALESCE(finished_at, ?), updated_at=?,
                 duration_ms=COALESCE(duration_ms, CAST((? - created_at) * 1000 AS INTEGER)),
                 error_type=COALESCE(error_type, 'expired'),
                 error_message=COALESCE(error_message, 'video task binding expired')
               WHERE media_type='video' AND status IN ('running', 'pending')
                 AND expires_at IS NOT NULL AND expires_at<=?""",
            (ts, ts, ts, ts),
        )
        conn.commit()
        return int(cur.rowcount or 0)


def media_recent(limit: int = 10, *, offset: int = 0) -> list[dict]:
    cleanup_expired_media()
    with _lock:
        rows = _get_conn().execute(
            "SELECT * FROM image_call_logs ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (max(1, int(limit)), max(0, int(offset))),
        ).fetchall()
    return [dict(row) for row in rows]


def media_count() -> int:
    cleanup_expired_media()
    with _lock:
        row = _get_conn().execute("SELECT COUNT(*) AS n FROM image_call_logs").fetchone()
    return int(row["n"] or 0) if row else 0


def media_summary() -> dict:
    cleanup_expired_media()
    with _lock:
        row = _get_conn().execute(
            """SELECT
                 COUNT(*) AS total,
                 SUM(CASE WHEN provider='openai' AND media_type='image' THEN 1 ELSE 0 END) AS openai_images,
                 SUM(CASE WHEN provider='xai' AND media_type='image' THEN 1 ELSE 0 END) AS xai_images,
                 SUM(CASE WHEN media_type='video' THEN 1 ELSE 0 END) AS videos,
                 SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
                 SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_count,
                 SUM(CASE WHEN status='expired' THEN 1 ELSE 0 END) AS expired_count,
                 SUM(CASE WHEN status IN ('running', 'pending') THEN 1 ELSE 0 END) AS pending_count,
                 SUM(COALESCE(image_count, 0)) AS image_count,
                 SUM(COALESCE(cost_usd_ticks, 0)) AS cost_usd_ticks,
                 AVG(CASE WHEN status='success' THEN duration_ms END) AS avg_duration_ms
               FROM image_call_logs"""
        ).fetchone()
    data = dict(row) if row else {}
    return {key: (value if value is not None else 0) for key, value in data.items()}


def media_account_top(limit: int = 5) -> list[dict]:
    cleanup_expired_media()
    with _lock:
        rows = _get_conn().execute(
            """SELECT
                 provider,
                 COALESCE(account_key, '') AS account_key,
                 COALESCE(account_email, '') AS account_email,
                 COUNT(*) AS total,
                 SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
                 SUM(CASE WHEN status IN ('failed', 'expired', 'cancelled') THEN 1 ELSE 0 END) AS failed_count,
                 SUM(COALESCE(cost_usd_ticks, 0)) AS cost_usd_ticks,
                 AVG(CASE WHEN duration_ms IS NOT NULL THEN duration_ms END) AS avg_duration_ms
               FROM image_call_logs
               WHERE COALESCE(account_key, '') != ''
               GROUP BY provider, account_key, account_email
               ORDER BY total DESC, success_count DESC
               LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
    return [dict(row) for row in rows]
