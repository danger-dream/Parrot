"""Codex reasoning replay 本地 Store（独立库 + 二级缓存）。

为什么需要：
  OAuth Codex 上游强制 `store=false`，多轮 agent 工具链里上游返回的
  reasoning 块（带 `encrypted_content` 加密推理签名）是续接推理链的关键。
  但 parrot 是无状态中转，**不能信任下游**会把 encrypted_content 原样带回——
  下游只要删了 reasoning 块，parrot 就永远拿不回来。

  因此采用 CPA(CLIProxyAPI) 验证过的思路：**不依赖下游**。上游每轮返回
  `response.completed` 时，自己把整批 assistant output items
  （reasoning / function_call / custom_tool_call）按 session_key 缓存；
  下一轮请求若下游没带 reasoning，自己按 session_key 取出整批、过滤后回填。
  下游传不传都无所谓——这才是健壮的。

存储模型（对齐 CPA codex_reasoning_replay_cache.go）：
  - key = (model, session_key)，一个会话一条记录（**不是按 call_id 一对一**）
  - value = 整批 output items（reasoning + 工具调用，按上游产出顺序）
  - 每轮 `response.completed` **整体覆盖**（不累积）
  - session_key 是连续性边界，**不绑上游账号**：auth failover 切账号时 replay 不丢

二级缓存（老大要求：换独立库 + 内存必须有）：
  - 内存 LRU 热层（条数 + 字节双上限，有界可驱逐）：活跃会话命中内存，O(1)
  - sqlite 独立库 `codex_reasoning.db` 持久兜底：冷会话、重启不丢
  - 写：内存 + sqlite 同步双写（reasoning 块仅几 KB，WAL 下亚毫秒级；不用
    后台 task「发射后不管」——SSE 生成器里它可能在执行前被请求结束/GC 丢掉）
  - 读：内存命中直接返回；未命中查库 + 回种内存

独立库（不污染 state.db，仿 image_db.py 惯例）：
  - 只存加密块整批 JSON，**不存历史消息正文** → 文件极小
  - 一行 = 一个会话最近一轮的 output items
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

from .. import config


# ─── 配置 ─────────────────────────────────────────────────────────


def _cfg() -> dict:
    return ((config.get().get("openai") or {}).get("reasoningStore")) or {}


def is_enabled() -> bool:
    # 默认开启；可通过 config.openai.reasoningStore.enabled=false 关闭
    return bool(_cfg().get("enabled", True))


def _ttl_seconds() -> int:
    minutes = int(_cfg().get("ttlMinutes", 60))
    return max(60, minutes * 60)


def _cleanup_interval_seconds() -> int:
    return int(_cfg().get("cleanupIntervalSeconds", 300))


def _mem_max_entries() -> int:
    return int(_cfg().get("memMaxEntries", 2048))


def _mem_max_bytes() -> int:
    return int(_cfg().get("memMaxBytes", 64 * 1024 * 1024))  # 64MB


# ─── 内存 LRU 热层（条数 + 字节双上限）────────────────────────────


class _LRU:
    """有界 LRU：按条数 + 总字节双上限驱逐最旧。值是 (items_json:str, expires_at:float)。"""

    def __init__(self) -> None:
        self._d: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()

    def get(self, key: str) -> Optional[tuple[str, float]]:
        with self._lock:
            v = self._d.get(key)
            if v is None:
                return None
            self._d.move_to_end(key)
            return v

    def put(self, key: str, items_json: str, expires_at: float) -> None:
        with self._lock:
            old = self._d.get(key)
            if old is not None:
                self._bytes -= len(old[0])
                self._d.move_to_end(key)
            self._d[key] = (items_json, expires_at)
            self._bytes += len(items_json)
            self._evict()

    def delete(self, key: str) -> None:
        with self._lock:
            old = self._d.pop(key, None)
            if old is not None:
                self._bytes -= len(old[0])

    def _evict(self) -> None:
        max_n = _mem_max_entries()
        max_b = _mem_max_bytes()
        while self._d and (len(self._d) > max_n or self._bytes > max_b):
            k, (v, _) = self._d.popitem(last=False)
            self._bytes -= len(v)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
            self._bytes = 0


_mem = _LRU()


# ─── sqlite 独立库 ────────────────────────────────────────────────


_local = threading.local()
_write_lock = threading.RLock()
_initialized = False
_db_path: Optional[str] = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS codex_reasoning (
  session_key   TEXT NOT NULL,
  model         TEXT NOT NULL,
  items_json    TEXT NOT NULL,
  created_at    REAL NOT NULL,
  expires_at    REAL NOT NULL,
  PRIMARY KEY (session_key, model)
);
CREATE INDEX IF NOT EXISTS idx_codex_reasoning_expires ON codex_reasoning(expires_at);
"""


def _resolve_db_path() -> str:
    raw = str(_cfg().get("dbPath") or "codex_reasoning.db").strip() or "codex_reasoning.db"
    if os.path.isabs(raw):
        return raw
    return os.path.join(config.DATA_DIR, raw)


def _get_conn() -> sqlite3.Connection:
    if getattr(_local, "conn", None) is None:
        if _db_path is None:
            raise RuntimeError("reasoning_store.init() not called")
        conn = sqlite3.connect(_db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _local.conn = conn
    return _local.conn


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
    print(f"[reasoning_store] Using {_db_path}")


# ─── session_key 构造 ─────────────────────────────────────────────


def make_session_key(api_key_name: str, prompt_cache_key: str) -> str:
    """会话连续性边界。复用 Codex OAuth 路径既有的隔离派生（不绑上游账号）。

    与 openai_oauth_channel._isolate_session_id 同源：按 api_key_name 隔离
    prompt_cache_key，避免同 OAuth 账户下不同下游 Key 串味。空则返回 ""。
    """
    api_key_name = (api_key_name or "").strip()
    prompt_cache_key = (prompt_cache_key or "").strip()
    if not prompt_cache_key:
        return ""
    try:
        from ..channel.openai_oauth_channel import _isolate_session_id
        iso = _isolate_session_id(api_key_name, prompt_cache_key)
        if iso:
            return iso
    except Exception:
        pass
    # 兜底：直接拼
    return f"{api_key_name}\x00{prompt_cache_key}" if api_key_name else prompt_cache_key


# ─── 归一化（对齐 CPA normalizeCodexReasoningReplayItem）────────────


def _is_nonempty_str(v: Any) -> bool:
    return isinstance(v, str) and v.strip() != "" and v == v.strip()


def normalize_items(raw_items: list) -> list[dict]:
    """从上游 output 里挑出可 replay 的 item，归一化成最小可回放形状。

    只保留三类：
      - reasoning：必须带合法非空 encrypted_content（store=false 续链命脉）
        归一化为 {type, summary:[], content:None, encrypted_content}
      - function_call：需 call_id + name + arguments(str)
      - custom_tool_call：需 call_id + name + input
    其余（message 等正文）不存——历史正文下游每轮会自带，不靠我们存。
    """
    out: list[dict] = []
    for it in raw_items or []:
        if not isinstance(it, dict):
            continue
        typ = str(it.get("type") or "").strip()
        if typ == "reasoning":
            enc = it.get("encrypted_content")
            if not _is_nonempty_str(enc):
                continue
            norm = {"type": "reasoning", "summary": [], "content": None,
                    "encrypted_content": enc}
            # 保留 summary（若上游给了非空摘要，原样带回更接近 codex 行为）
            summ = it.get("summary")
            if isinstance(summ, list) and summ:
                norm["summary"] = summ
            out.append(norm)
        elif typ == "function_call":
            call_id = str(it.get("call_id") or "").strip()
            name = str(it.get("name") or "").strip()
            args = it.get("arguments")
            if not call_id or not name or not isinstance(args, str):
                continue
            out.append({"type": "function_call", "call_id": call_id,
                        "name": name, "arguments": args})
        elif typ == "custom_tool_call":
            call_id = str(it.get("call_id") or "").strip()
            name = str(it.get("name") or "").strip()
            inp = it.get("input")
            if not call_id or not name or inp is None:
                continue
            norm = {"type": "custom_tool_call", "status": str(it.get("status") or "completed"),
                    "call_id": call_id, "name": name, "input": inp}
            out.append(norm)
    return out


# ─── 写（捕获）─────────────────────────────────────────────────────


def _save_sync(session_key: str, model: str, items_json: str, ttl: int) -> None:
    if not _initialized:
        return
    now = time.time()
    expires_at = now + ttl
    conn = _get_conn()
    with _write_lock:
        conn.execute(
            """INSERT OR REPLACE INTO codex_reasoning
               (session_key, model, items_json, created_at, expires_at)
               VALUES (?,?,?,?,?)""",
            (session_key, model or "", items_json, now, expires_at),
        )
        conn.commit()


def save_items(session_key: str, model: str, raw_items: list) -> int:
    """捕获上游整批 output（内存 + sqlite 同步双写）。返回归一化后存的条数。

    每轮 response.completed 调一次，有新 reasoning/工具调用则整体覆盖该 session
    的记录；这一轮归一化为空（如 agent 最后一轮纯文本汇总）则**保留**旧缓存，
    不删——下一轮追问仍需靠它续链。
    """
    if not is_enabled() or not session_key:
        return 0
    norm = normalize_items(raw_items)
    ttl = _ttl_seconds()
    expires_at = time.time() + ttl
    key = _ck(session_key, model)
    if not norm:
        # 这一轮上游没产出新的 reasoning/工具调用（如 agent 最后一轮纯文本汇总）。
        # 关键：**保留**该会话已有缓存，绝不删——下一轮用户追问仍要靠上几轮的
        # reasoning 续链。CPA 语义是「有新的才覆盖」，不是「空了就清」。
        # （删除只在 invalidate：上游明确拒绝 encrypted_content 时发生。）
        return 0
    items_json = json.dumps(norm, ensure_ascii=False, separators=(",", ":"))
    # 内存热层立即可见
    _mem.put(key, items_json, expires_at)
    # sqlite 同步直写：reasoning 块仅几 KB，WAL 模式下写入亚毫秒级，不阻塞主流；
    # 不用 create_task「发射后不管」——SSE 生成器里后台 task 可能在执行前就被
    # 请求结束/GC 丢掉，导致落库丢失（实测踩过）。
    if _initialized:
        try:
            _save_sync(session_key, model, items_json, ttl)
        except Exception as exc:
            # 落库失败：内存热层仍可用（本会话续链不受影响），但重启会丢。
            # 记日志便于发现持久层异常，不抛出（绝不阻断主请求）。
            print(f"[reasoning_store] save failed (mem still ok): {exc}")
    return len(norm)


# ─── 读（回填）─────────────────────────────────────────────────────


def get_items(session_key: str, model: str) -> list[dict]:
    """二级查询：内存 LRU → sqlite → 回种内存。过期/不存在返回 []。"""
    if not is_enabled() or not session_key:
        return []
    key = _ck(session_key, model)
    now = time.time()
    # 1) 内存
    hit = _mem.get(key)
    if hit is not None:
        items_json, expires_at = hit
        if expires_at >= now:
            try:
                return json.loads(items_json)
            except Exception:
                return []
        _mem.delete(key)
    # 2) sqlite
    if not _initialized:
        return []
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT items_json, expires_at FROM codex_reasoning WHERE session_key=? AND model=?",
            (session_key, model or ""),
        ).fetchone()
    except Exception:
        return []
    if row is None:
        return []
    if float(row["expires_at"]) < now:
        return []
    items_json = row["items_json"]
    # 回种内存
    _mem.put(key, items_json, float(row["expires_at"]))
    try:
        return json.loads(items_json)
    except Exception:
        return []


def _delete_sync(session_key: str, model: str) -> None:
    if not _initialized:
        return
    conn = _get_conn()
    with _write_lock:
        conn.execute(
            "DELETE FROM codex_reasoning WHERE session_key=? AND model=?",
            (session_key, model or ""),
        )
        conn.commit()


def invalidate(session_key: str, model: str) -> None:
    """上游拒绝 encrypted_content（invalid_encrypted_content / 签名失效）时清记录。"""
    if not session_key:
        return
    _mem.delete(_ck(session_key, model))
    if _initialized:
        try:
            _delete_sync(session_key, model)
        except Exception:
            pass


def _ck(session_key: str, model: str) -> str:
    return f"{model or ''}\x00{session_key}"


# ─── 回填核心：把缓存 items 过滤 + 插入到下游 input ────────────────


def _comparable_call_ids(call_id: str) -> list[str]:
    """call_id 可能带/不带 fc 前缀，生成可比较的候选集（对齐 codex 前缀规范化）。"""
    cid = (call_id or "").strip()
    if not cid:
        return []
    out = {cid}
    if cid.startswith("call_"):
        out.add("fc" + cid[len("call_"):])
    if cid.startswith("fc_"):
        out.add("call_" + cid[len("fc_"):])
    if cid.startswith("fc") and not cid.startswith("fc_"):
        out.add("call_" + cid[len("fc"):])
    return list(out)


def _input_has_valid_reasoning(input_items: list) -> bool:
    """下游 input 里是否已带合法 encrypted reasoning（带了就不补，Fix A 快路径）。"""
    for it in input_items or []:
        if isinstance(it, dict) and str(it.get("type") or "") == "reasoning":
            if _is_nonempty_str(it.get("encrypted_content")):
                return True
    return False


def backfill_input(input_items: list, cached_items: list[dict]) -> tuple[list, int]:
    """把缓存 items 过滤后插回 input（对齐 CPA filter+insert）。

    规则：
      - reasoning：input 里已有合法 encrypted reasoning → 整批 reasoning 都不补；
        否则补（下游删了，我们补回）
      - function_call/custom_tool_call：仅当 input 里有配对的 *_output 但缺这个 call
        时补（避免补出孤儿 tool_call）
      - 插入点：第一个 function_call_output / custom_tool_call_output 之前；
        没有则插在末尾
    返回 (新 input, 补入条数)。补 0 条则返回原 input。
    """
    if not cached_items or not isinstance(input_items, list):
        return input_items, 0

    has_input_reasoning = _input_has_valid_reasoning(input_items)
    existing_calls: set[str] = set()
    existing_outputs: set[str] = set()
    has_tool_call = False
    for it in input_items:
        if not isinstance(it, dict):
            continue
        typ = str(it.get("type") or "")
        if typ in ("function_call_output", "custom_tool_call_output"):
            cid = str(it.get("call_id") or "").strip()
            for c in _comparable_call_ids(cid):
                existing_outputs.add(c)
        if typ in ("function_call", "custom_tool_call"):
            has_tool_call = True
            cid = str(it.get("call_id") or "").strip()
            for c in _comparable_call_ids(cid):
                existing_calls.add(c)

    filtered: list[dict] = []
    for it in cached_items:
        typ = str(it.get("type") or "")
        if typ == "reasoning":
            # reasoning 块是为工具调用链续推理服务的：仅当本轮 input 确实带了工具
            # 调用、且没有自带合法 reasoning 时才补。纯对话/新话题（无任何 tool_call）
            # 不补——否则会把孤立 reasoning 块插到末尾，上游可能拒绝或行为异常。
            if has_input_reasoning or not has_tool_call:
                continue
            filtered.append(it)
        elif typ in ("function_call", "custom_tool_call"):
            cids = _comparable_call_ids(str(it.get("call_id") or ""))
            if not cids:
                continue
            # 已存在该 call → 跳过
            if any(c in existing_calls for c in cids):
                continue
            # 只在有配对 output 时补（否则是孤儿）
            if not any(c in existing_outputs for c in cids):
                continue
            for c in cids:
                existing_calls.add(c)
            filtered.append(it)

    if not filtered:
        return input_items, 0

    # 插入点：第一个 *_output 之前
    insert_idx = len(input_items)
    for i, it in enumerate(input_items):
        if isinstance(it, dict) and str(it.get("type") or "") in (
                "function_call_output", "custom_tool_call_output"):
            insert_idx = i
            break

    new_input = list(input_items[:insert_idx]) + filtered + list(input_items[insert_idx:])
    return new_input, len(filtered)


# ─── 清理 ─────────────────────────────────────────────────────────


def cleanup_expired(now: Optional[float] = None) -> int:
    if not _initialized:
        return 0
    conn = _get_conn()
    with _write_lock:
        cur = conn.execute(
            "DELETE FROM codex_reasoning WHERE expires_at < ?",
            (now if now is not None else time.time(),),
        )
        conn.commit()
        return cur.rowcount or 0


async def cleanup_loop() -> None:
    while True:
        try:
            interval = _cleanup_interval_seconds()
        except Exception:
            interval = 300
        await asyncio.sleep(max(10, interval))
        try:
            cleared = await asyncio.to_thread(cleanup_expired)
            if cleared:
                print(f"[reasoning_store] cleaned {cleared} expired entries")
        except Exception as exc:
            print(f"[reasoning_store] cleanup failed: {exc}")


# ─── 测试辅助 ─────────────────────────────────────────────────────


def _reset_for_test() -> None:
    _mem.clear()
    if _initialized:
        conn = _get_conn()
        with _write_lock:
            conn.execute("DELETE FROM codex_reasoning")
            conn.commit()
