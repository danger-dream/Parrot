"""亲和绑定：fingerprint → (channel_key, model)。

内存 + StateStore snapshots 双层：
  - 内存：快速查找
  - StateStore snapshots：重启恢复

首次调用 init() 从 StateStore snapshots 全量加载到内存。成功路径的 upsert 先持久化再
发布到内存；SQLite 可用性失败时跳过内存更新，避免进程状态与重启状态分叉。
过期清理由后台 loop 调用 cleanup()。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Iterable, Optional

from . import channel_state, config, state_db
from .sqlite_errors import is_availability_error


_lock = threading.Lock()
# Serialize DB commit + matching memory publication without blocking read-only get().
# state_db already serializes physical writes globally; this lock additionally
# preserves the order observed by this module's in-memory mirror.
_mutation_lock = channel_state.mutation_lock
_entries: dict[str, dict] = {}  # fingerprint -> {channel_key, model, last_used}
_initialized = False
_logger = logging.getLogger(__name__)
_warning_lock = threading.Lock()
_last_warning_at: dict[str, float] = {}
_WARNING_INTERVAL_SECONDS = 60.0


def _persist_optional(name: str, effect) -> bool:
    """Persist optional affinity state without corrupting in-memory state.

    Availability failures skip the matching memory mutation so both layers
    remain consistent. Programming/schema/constraint failures stay visible.
    """
    try:
        with state_db.optional_write_timeout():
            effect()
        return True
    except sqlite3.Error as exc:
        if not is_availability_error(exc):
            raise
        now = time.monotonic()
        with _warning_lock:
            last = _last_warning_at.get(name, 0.0)
            should_warn = not last or now - last >= _WARNING_INTERVAL_SECONDS
            if should_warn:
                _last_warning_at[name] = now
        if should_warn:
            _logger.warning(
                "affinity persistence %s failed; memory update skipped: %s", name, exc,
            )
        return False


def init() -> None:
    """从 StateStore snapshots 加载全部亲和记录到内存。"""
    global _initialized
    with _mutation_lock:
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
    print(f"[affinity] loaded {len(rows)} entries from StateStore snapshots")


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
        # Re-check while excluding mutations.  An upsert may have refreshed the
        # same fingerprint after the optimistic read above; never delete that
        # freshly published binding based on the stale snapshot.
        with _mutation_lock:
            with _lock:
                current = _entries.get(fingerprint)
            if current is None:
                return None
            if now - current["last_used"] <= _ttl_ms():
                return dict(current)
            if _persist_optional(
                "delete_expired", lambda: state_db.affinity_delete(fingerprint),
            ):
                with _lock:
                    if _entries.get(fingerprint) is current:
                        _entries.pop(fingerprint, None)
        return None
    return dict(entry)


def upsert(fingerprint: Optional[str], channel_key: str, model: str,
           prompt_cache_key: Optional[str] = None) -> None:
    """插入或更新绑定。内存 + StateStore snapshots 双写。

    prompt_cache_key 仅供 OpenAI 协议自动补 `prompt_cache_key` 使用；
    传 None 表示保留旧值，不影响 Anthropic/其他协议的亲和语义。
    """
    if not fingerprint:
        return
    now = state_db.now_ms()
    with _mutation_lock:
        if channel_state.is_deleted(channel_key):
            return
        channel_key = channel_state.resolve(channel_key)
        if channel_state.is_deleted(channel_key):
            return
        if not _persist_optional(
            "upsert",
            lambda: state_db.affinity_upsert(
                fingerprint, channel_key, model, last_used=now,
                prompt_cache_key=prompt_cache_key,
            ),
        ):
            return
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


def touch(fingerprint: Optional[str]) -> None:
    """仅更新 last_used。命中时调用以延续 TTL。"""
    if not fingerprint:
        return
    now = state_db.now_ms()
    with _mutation_lock:
        with _lock:
            exists = fingerprint in _entries
        if not exists:
            return
        if not _persist_optional(
            "touch",
            lambda: state_db.affinity_touch(fingerprint, last_used=now),
        ):
            return
        with _lock:
            entry = _entries.get(fingerprint)
            if entry is not None:
                entry["last_used"] = now


def delete(fingerprint: Optional[str]) -> None:
    if not fingerprint:
        return
    with _mutation_lock:
        if not _persist_optional(
            "delete", lambda: state_db.affinity_delete(fingerprint),
        ):
            return
        with _lock:
            _entries.pop(fingerprint, None)


def delete_all() -> None:
    with _mutation_lock:
        if not _persist_optional("delete_all", lambda: state_db.affinity_delete(None)):
            return
        with _lock:
            _entries.clear()


def delete_by_channel(channel_key: str) -> None:
    with _mutation_lock:
        if not _persist_optional(
            "delete_by_channel",
            lambda: state_db.affinity_delete_by_channel(channel_key),
        ):
            return
        with _lock:
            keys = [k for k, v in _entries.items() if v["channel_key"] == channel_key]
            for k in keys:
                _entries.pop(k, None)


def delete_stale_channels(live_keys: Iterable[str]) -> int:
    live_set = set(live_keys)
    with _mutation_lock:
        if not _persist_optional(
            "delete_stale_channels",
            lambda: state_db.affinity_delete_stale_channels(live_set),
        ):
            return 0
        with _lock:
            stale = [
                key for key, value in _entries.items()
                if value["channel_key"] not in live_set
            ]
            for key in stale:
                _entries.pop(key, None)
        return len(stale)


def delete_by_protocol(protocol_family: str) -> int:
    """按协议家族（anthropic/openai）清理 fp 亲和。返回清理数量。

    用渠道注册表把所有该家族的 channel_key 找出来，逐个调
    `state_db.affinity_delete_by_channel`，避免内存表里再额外存 protocol 字段。
    """
    from . import load_balancing
    from .channel import registry
    keys = {
        ch.key for ch in registry.all_channels()
        if load_balancing.family_for_channel(ch) == protocol_family
    }
    if not keys:
        return 0
    removed = 0
    with _mutation_lock:
        for ch_key in keys:
            if not _persist_optional(
                "delete_by_protocol",
                lambda ch_key=ch_key: state_db.affinity_delete_by_channel(ch_key),
            ):
                continue
            with _lock:
                targets = [
                    k for k, v in _entries.items()
                    if v["channel_key"] == ch_key
                ]
                for key in targets:
                    _entries.pop(key, None)
                removed += len(targets)
    return removed


def rename_channel(old_key: str, new_key: str, *, persist: bool = True) -> bool:
    if old_key == new_key:
        return True
    with _mutation_lock:
        if persist and not _persist_optional(
            "rename_channel",
            lambda: state_db.affinity_rename_channel(old_key, new_key),
        ):
            return False
        with _lock:
            for entry in _entries.values():
                if entry["channel_key"] == old_key:
                    entry["channel_key"] = new_key
        return True


def cleanup(ttl_ms: Optional[int] = None) -> int:
    """清理 last_used 早于 now-ttl 的记录。返回清理数量。"""
    if ttl_ms is None:
        ttl_ms = _ttl_ms()
    cutoff = state_db.now_ms() - ttl_ms
    with _mutation_lock:
        if not _persist_optional(
            "cleanup",
            lambda: state_db.affinity_cleanup(ttl_ms, cutoff_ms=cutoff),
        ):
            return 0
        with _lock:
            stale = [k for k, v in _entries.items() if v["last_used"] < cutoff]
            for k in stale:
                _entries.pop(k, None)
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
_client_mutation_lock = channel_state.mutation_lock
_client_entries: dict[str, dict] = {}  # client_key -> {channel_key, model, last_used}
_client_initialized = False


def _client_ttl_ms() -> int:
    cfg = config.get()
    return int(cfg.get("affinity", {}).get("clientTtlMinutes", 120) * 60 * 1000)


def client_init() -> None:
    """从 StateStore snapshots 加载全部 client 亲和记录到内存。"""
    global _client_initialized
    with _client_mutation_lock:
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
    print(f"[affinity] loaded {len(rows)} client entries from StateStore snapshots")


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
        with _client_mutation_lock:
            with _client_lock:
                current = _client_entries.get(client_key)
            if current is None:
                return None
            if now - current["last_used"] <= _client_ttl_ms():
                return dict(current)
            if _persist_optional(
                "client_delete_expired",
                lambda: state_db.client_affinity_delete(client_key),
            ):
                with _client_lock:
                    if _client_entries.get(client_key) is current:
                        _client_entries.pop(client_key, None)
        return None
    return dict(entry)


def client_upsert(client_key: str, channel_key: str, model: str) -> None:
    """插入或更新 client 绑定。内存 + StateStore snapshots 双写。"""
    if not client_key:
        return
    now = state_db.now_ms()
    with _client_mutation_lock:
        if channel_state.is_deleted(channel_key):
            return
        channel_key = channel_state.resolve(channel_key)
        if channel_state.is_deleted(channel_key):
            return
        if not _persist_optional(
            "client_upsert",
            lambda: state_db.client_affinity_upsert(
                client_key, channel_key, model, last_used=now,
            ),
        ):
            return
        with _client_lock:
            _client_entries[client_key] = {
                "channel_key": channel_key,
                "model": model,
                "last_used": now,
            }


def client_delete(client_key: str) -> None:
    if not client_key:
        return
    with _client_mutation_lock:
        if not _persist_optional(
            "client_delete", lambda: state_db.client_affinity_delete(client_key),
        ):
            return
        with _client_lock:
            _client_entries.pop(client_key, None)


def client_delete_all() -> None:
    with _client_mutation_lock:
        if not _persist_optional(
            "client_delete_all", lambda: state_db.client_affinity_delete(None),
        ):
            return
        with _client_lock:
            _client_entries.clear()


def client_delete_by_channel(channel_key: str) -> None:
    with _client_mutation_lock:
        if not _persist_optional(
            "client_delete_by_channel",
            lambda: state_db.client_affinity_delete_by_channel(channel_key),
        ):
            return
        with _client_lock:
            keys = [
                k for k, v in _client_entries.items()
                if v["channel_key"] == channel_key
            ]
            for key in keys:
                _client_entries.pop(key, None)


def client_delete_stale_channels(live_keys: Iterable[str]) -> int:
    live_set = set(live_keys)
    with _client_mutation_lock:
        if not _persist_optional(
            "client_delete_stale_channels",
            lambda: state_db.client_affinity_delete_stale_channels(live_set),
        ):
            return 0
        with _client_lock:
            stale = [
                key for key, value in _client_entries.items()
                if value["channel_key"] not in live_set
            ]
            for key in stale:
                _client_entries.pop(key, None)
        return len(stale)


def client_delete_by_protocol(protocol_family: str) -> int:
    """按协议家族清理 client 亲和。返回清理数量。"""
    from . import load_balancing
    from .channel import registry
    keys = {
        ch.key for ch in registry.all_channels()
        if load_balancing.family_for_channel(ch) == protocol_family
    }
    if not keys:
        return 0
    removed = 0
    with _client_mutation_lock:
        for ch_key in keys:
            if not _persist_optional(
                "client_delete_by_protocol",
                lambda ch_key=ch_key: state_db.client_affinity_delete_by_channel(ch_key),
            ):
                continue
            with _client_lock:
                targets = [
                    k for k, v in _client_entries.items()
                    if v["channel_key"] == ch_key
                ]
                for key in targets:
                    _client_entries.pop(key, None)
                removed += len(targets)
    return removed


def client_rename_channel(old_key: str, new_key: str, *, persist: bool = True) -> bool:
    if old_key == new_key:
        return True
    with _client_mutation_lock:
        if persist and not _persist_optional(
            "client_rename_channel",
            lambda: state_db.client_affinity_rename_channel(old_key, new_key),
        ):
            return False
        with _client_lock:
            for entry in _client_entries.values():
                if entry["channel_key"] == old_key:
                    entry["channel_key"] = new_key
        return True


def client_cleanup(ttl_ms: Optional[int] = None) -> int:
    if ttl_ms is None:
        ttl_ms = _client_ttl_ms()
    cutoff = state_db.now_ms() - ttl_ms
    with _client_mutation_lock:
        if not _persist_optional(
            "client_cleanup",
            lambda: state_db.client_affinity_cleanup(ttl_ms, cutoff_ms=cutoff),
        ):
            return 0
        with _client_lock:
            stale = [k for k, v in _client_entries.items() if v["last_used"] < cutoff]
            for k in stale:
                _client_entries.pop(k, None)
        return len(stale)


def client_count() -> int:
    with _client_lock:
        return len(_client_entries)


def client_snapshot() -> dict[str, dict]:
    with _client_lock:
        return {k: dict(v) for k, v in _client_entries.items()}
