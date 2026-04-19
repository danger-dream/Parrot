"""亲和绑定：fingerprint → (channel_key, model)。

内存 + state.db 双层：
  - 内存：快速查找
  - state.db：重启恢复

首次调用 init() 从 state.db 全量加载到内存。之后所有写操作双写。
过期清理由后台 loop 调用 cleanup()。
"""

from __future__ import annotations

import threading
from typing import Optional

from . import config, state_db


_lock = threading.Lock()
_entries: dict[str, dict] = {}  # fingerprint -> {channel_key, model, last_used}
_initialized = False


def init() -> None:
    """从 state.db 加载全部亲和记录到内存。"""
    global _initialized
    if _initialized:
        return
    rows = state_db.affinity_load_all()
    with _lock:
        _entries.clear()
        for row in rows:
            _entries[row["fingerprint"]] = {
                "channel_key": row["channel_key"],
                "model": row["model"],
                "last_used": row["last_used"],
            }
    _initialized = True
    print(f"[affinity] loaded {len(rows)} entries from state.db")


def _ttl_ms() -> int:
    cfg = config.get()
    return int(cfg.get("affinity", {}).get("ttlMinutes", 30) * 60 * 1000)


def get(fingerprint: Optional[str]) -> Optional[dict]:
    """查询一条绑定。若已过期自动删除。"""
    if not fingerprint:
        return None
    with _lock:
        entry = _entries.get(fingerprint)
    if not entry:
        return None
    now = state_db.now_ms()
    if now - entry["last_used"] > _ttl_ms():
        delete(fingerprint)
        return None
    return dict(entry)


def upsert(fingerprint: Optional[str], channel_key: str, model: str) -> None:
    """插入或更新绑定。内存 + state.db 双写。"""
    if not fingerprint:
        return
    now = state_db.now_ms()
    with _lock:
        _entries[fingerprint] = {
            "channel_key": channel_key,
            "model": model,
            "last_used": now,
        }
    state_db.affinity_upsert(fingerprint, channel_key, model, last_used=now)


def touch(fingerprint: Optional[str]) -> None:
    """仅更新 last_used。命中时调用以延续 TTL。"""
    if not fingerprint:
        return
    now = state_db.now_ms()
    changed = False
    with _lock:
        entry = _entries.get(fingerprint)
        if entry is not None:
            entry["last_used"] = now
            changed = True
    if changed:
        state_db.affinity_touch(fingerprint, last_used=now)


def delete(fingerprint: Optional[str]) -> None:
    if not fingerprint:
        return
    with _lock:
        _entries.pop(fingerprint, None)
    state_db.affinity_delete(fingerprint)


def delete_all() -> None:
    with _lock:
        _entries.clear()
    state_db.affinity_delete(None)


def delete_by_channel(channel_key: str) -> None:
    with _lock:
        keys = [k for k, v in _entries.items() if v["channel_key"] == channel_key]
        for k in keys:
            _entries.pop(k, None)
    state_db.affinity_delete_by_channel(channel_key)


def rename_channel(old_key: str, new_key: str) -> None:
    if old_key == new_key:
        return
    with _lock:
        for entry in _entries.values():
            if entry["channel_key"] == old_key:
                entry["channel_key"] = new_key
    state_db.affinity_rename_channel(old_key, new_key)


def cleanup(ttl_ms: Optional[int] = None) -> int:
    """清理 last_used 早于 now-ttl 的记录。返回清理数量。"""
    if ttl_ms is None:
        ttl_ms = _ttl_ms()
    cutoff = state_db.now_ms() - ttl_ms
    with _lock:
        stale = [k for k, v in _entries.items() if v["last_used"] < cutoff]
        for k in stale:
            _entries.pop(k, None)
    # state.db 单独清（避免每条都调一次 DELETE）
    state_db.affinity_cleanup(ttl_ms)
    return len(stale)


def count() -> int:
    with _lock:
        return len(_entries)


def snapshot() -> dict[str, dict]:
    """调试/TG 展示用。返回内存中所有绑定的只读快照。"""
    with _lock:
        return {k: dict(v) for k, v in _entries.items()}
