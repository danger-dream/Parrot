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
                "prompt_cache_key": row.get("prompt_cache_key"),
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


def upsert(fingerprint: Optional[str], channel_key: str, model: str,
           prompt_cache_key: Optional[str] = None) -> None:
    """插入或更新绑定。内存 + state.db 双写。

    prompt_cache_key 仅供 OpenAI 协议自动补 `prompt_cache_key` 使用；
    传 None 表示保留旧值，不影响 Anthropic/其他协议的亲和语义。
    """
    if not fingerprint:
        return
    now = state_db.now_ms()
    with _lock:
        prev = _entries.get(fingerprint) or {}
        entry = {
            "channel_key": channel_key,
            "model": model,
            "last_used": now,
            "prompt_cache_key": (
                prompt_cache_key if prompt_cache_key is not None
                else prev.get("prompt_cache_key")
            ),
        }
        _entries[fingerprint] = entry
    state_db.affinity_upsert(
        fingerprint, channel_key, model, last_used=now,
        prompt_cache_key=prompt_cache_key,
    )


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


# ═══════════════════════════════════════════════════════════════
# Client-level soft affinity: (api_key_name, client_ip, model) → channel
#
# 作用：当 fingerprint 亲和不可用时（新会话 < 3 消息、fp 过期）提供
# 回退绑定，让同一客户端的请求尽量粘到最近使用的渠道，提高上游
# prefix cache 命中率。
#
# TTL 独立于 fp 亲和（默认 120 分钟，可通过
# config.affinity.clientTtlMinutes 调整）。
# ═══════════════════════════════════════════════════════════════

_client_lock = threading.Lock()
_client_entries: dict[str, dict] = {}  # client_key -> {channel_key, model, last_used}
_client_initialized = False


def _client_ttl_ms() -> int:
    cfg = config.get()
    return int(cfg.get("affinity", {}).get("clientTtlMinutes", 120) * 60 * 1000)


def client_init() -> None:
    """从 state.db 加载全部 client 亲和记录到内存。"""
    global _client_initialized
    if _client_initialized:
        return
    rows = state_db.client_affinity_load_all()
    with _client_lock:
        _client_entries.clear()
        for row in rows:
            _client_entries[row["client_key"]] = {
                "channel_key": row["channel_key"],
                "model": row["model"],
                "last_used": row["last_used"],
            }
    _client_initialized = True
    print(f"[affinity] loaded {len(rows)} client entries from state.db")


def make_client_key(api_key_name: str, client_ip: str, model: str) -> str:
    """构造 client affinity 的 key。"""
    return f"{api_key_name or '-'}|{client_ip or '-'}|{model or '-'}"


def client_get(client_key: str) -> Optional[dict]:
    """查询 client 绑定。若已过期自动删除。"""
    if not client_key:
        return None
    with _client_lock:
        entry = _client_entries.get(client_key)
    if not entry:
        return None
    now = state_db.now_ms()
    if now - entry["last_used"] > _client_ttl_ms():
        client_delete(client_key)
        return None
    return dict(entry)


def client_upsert(client_key: str, channel_key: str, model: str) -> None:
    """插入或更新 client 绑定。内存 + state.db 双写。"""
    if not client_key:
        return
    now = state_db.now_ms()
    with _client_lock:
        _client_entries[client_key] = {
            "channel_key": channel_key,
            "model": model,
            "last_used": now,
        }
    state_db.client_affinity_upsert(client_key, channel_key, model, last_used=now)


def client_delete(client_key: str) -> None:
    if not client_key:
        return
    with _client_lock:
        _client_entries.pop(client_key, None)
    state_db.client_affinity_delete(client_key)


def client_delete_all() -> None:
    with _client_lock:
        _client_entries.clear()
    state_db.client_affinity_delete(None)


def client_delete_by_channel(channel_key: str) -> None:
    with _client_lock:
        keys = [k for k, v in _client_entries.items() if v["channel_key"] == channel_key]
        for k in keys:
            _client_entries.pop(k, None)
    state_db.client_affinity_delete_by_channel(channel_key)


def client_rename_channel(old_key: str, new_key: str) -> None:
    if old_key == new_key:
        return
    with _client_lock:
        for entry in _client_entries.values():
            if entry["channel_key"] == old_key:
                entry["channel_key"] = new_key
    state_db.client_affinity_rename_channel(old_key, new_key)


def client_cleanup(ttl_ms: Optional[int] = None) -> int:
    if ttl_ms is None:
        ttl_ms = _client_ttl_ms()
    cutoff = state_db.now_ms() - ttl_ms
    with _client_lock:
        stale = [k for k, v in _client_entries.items() if v["last_used"] < cutoff]
        for k in stale:
            _client_entries.pop(k, None)
    state_db.client_affinity_cleanup(ttl_ms)
    return len(stale)


def client_count() -> int:
    with _client_lock:
        return len(_client_entries)


def client_snapshot() -> dict[str, dict]:
    with _client_lock:
        return {k: dict(v) for k, v in _client_entries.items()}
