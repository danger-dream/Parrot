"""`previous_response_id` 本地 Store。

挂在既有 `state.db` 上的一张独立表（`openai_response_store`），与 anthropic 侧
表名隔离。Responses API 是有状态的：客户端可以用 `previous_response_id`
续接历史；当上游是 openai-chat（无状态）时，proxy 必须本地展开历史并翻成
chat messages 送给上游。

模块级单例：
  - `init()` 启动时调一次；失败则抛 RuntimeError
  - `save(...)` / `lookup(...)` / `expand_history(...)` 接口
  - `cleanup_expired()` / `cleanup_loop()` 后台清理

并发：独立的 thread-local sqlite 连接 + 模块级 RLock 序列化写；SQLite WAL
模式已处理跨进程（单进程）/跨线程的读写隔离。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from .. import config


# ─── 异常 ─────────────────────────────────────────────────────────


class ResponseNotFound(Exception):
    pass


class ResponseExpired(Exception):
    pass


class ResponseForbidden(Exception):
    """api_key_name 与 Store 中记录的不一致 —— 防 Key 间碰撞。"""
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


def _resolve_db_path() -> str:
    """与 state_db 共用同一数据库文件。"""
    cfg = config.get()
    rel = cfg.get("stateDbPath", "state.db")
    if os.path.isabs(rel):
        return rel
    return os.path.join(config.DATA_DIR, rel)


def _get_conn() -> sqlite3.Connection:
    if getattr(_local, "conn", None) is None:
        if _db_path is None:
            raise RuntimeError("openai.store.init() not called")
        conn = sqlite3.connect(_db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _local.conn = conn
    return _local.conn


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
    global _initialized, _db_path
    if _initialized:
        return
    _db_path = _resolve_db_path()
    os.makedirs(os.path.dirname(_db_path) or ".", exist_ok=True)
    conn = _get_conn()
    with _write_lock:
        conn.executescript(_SCHEMA)
        conn.commit()
    _initialized = True
    print(f"[openai_store] Using {_db_path}")


# ─── 配置访问 ────────────────────────────────────────────────────


def _store_cfg() -> dict:
    return (config.get().get("openai") or {}).get("store") or {}


def is_enabled() -> bool:
    return bool(_store_cfg().get("enabled", True))


def _ttl_seconds() -> int:
    minutes = int(_store_cfg().get("ttlMinutes", 60))
    return max(60, minutes * 60)


def _cleanup_interval_seconds() -> int:
    return int(_store_cfg().get("cleanupIntervalSeconds", 300))


# ─── CRUD ────────────────────────────────────────────────────────


def save(response_id: str, parent_id: Optional[str], *,
         api_key_name: str, model: str, channel_key: Optional[str],
         input_items: list, output_items: list,
         ttl_seconds: Optional[int] = None) -> None:
    if not _initialized:
        # Store 未初始化时静默跳过，避免阻塞请求主路径
        return
    now = time.time()
    ttl = ttl_seconds if ttl_seconds is not None else _ttl_seconds()
    expires_at = now + ttl
    conn = _get_conn()
    with _write_lock:
        conn.execute(
            """INSERT OR REPLACE INTO openai_response_store
               (response_id, parent_id, api_key_name, model, channel_key,
                created_at, expires_at, input_items, output_items)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                response_id, parent_id, api_key_name or "", model or "",
                channel_key or "", now, expires_at,
                json.dumps(input_items, ensure_ascii=False),
                json.dumps(output_items, ensure_ascii=False),
            ),
        )
        conn.commit()


def lookup(response_id: str, *, api_key_name: str) -> StoredResponse:
    if not _initialized:
        raise ResponseNotFound(response_id)
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM openai_response_store WHERE response_id=?",
        (response_id,),
    ).fetchone()
    if row is None:
        raise ResponseNotFound(response_id)
    if row["api_key_name"] != (api_key_name or ""):
        raise ResponseForbidden(response_id)
    if row["expires_at"] is not None and float(row["expires_at"]) < time.time():
        raise ResponseExpired(response_id)
    try:
        input_items = json.loads(row["input_items"]) if row["input_items"] else []
        output_items = json.loads(row["output_items"]) if row["output_items"] else []
    except Exception:
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


def expand_history(response_id: str, *, api_key_name: str,
                   max_depth: int = 50) -> list[dict]:
    """沿 parent_id 链向上展开，返回 items 列表（老→新；`input_items + output_items` 连接）。

    链循环（防御性）或深度超 `max_depth` 时截断，返回已收集的部分。
    """
    chain: list[StoredResponse] = []
    seen: set[str] = set()
    cur: Optional[str] = response_id
    depth = 0
    while cur and cur not in seen and depth < max_depth:
        seen.add(cur)
        rec = lookup(cur, api_key_name=api_key_name)
        chain.append(rec)
        cur = rec.parent_id
        depth += 1
    chain.reverse()
    items: list[dict] = []
    for rec in chain:
        items.extend(rec.input_items)
        items.extend(rec.output_items)
    return items


def cleanup_expired(now: Optional[float] = None) -> int:
    """清理过期记录，返回清理数量。"""
    if not _initialized:
        return 0
    conn = _get_conn()
    with _write_lock:
        cur = conn.execute(
            "DELETE FROM openai_response_store WHERE expires_at < ?",
            (now if now is not None else time.time(),),
        )
        conn.commit()
        return cur.rowcount or 0


async def cleanup_loop() -> None:
    """后台循环：每 `cleanupIntervalSeconds` 跑一次 cleanup_expired。"""
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
            print(f"[openai_store] cleanup failed: {exc}")


# ─── 测试辅助 ────────────────────────────────────────────────────


def _reset_for_test() -> None:
    """仅测试用：清空表（保留 schema）。"""
    if not _initialized:
        return
    conn = _get_conn()
    with _write_lock:
        conn.execute("DELETE FROM openai_response_store")
        conn.commit()
