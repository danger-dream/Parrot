"""v3 Codex reasoning replay store 测试套件。

覆盖：normalize / LRU 二级缓存 / sqlite 持久 / session_key / save_items 语义
（含「最后一轮纯文本不删旧缓存」的回归用例）/ backfill 回填过滤+插入 / invalidate。
"""
import os
import time
import tempfile
import importlib

import pytest

# 用独立临时库，避免污染
import src.config as config
from src.openai import reasoning_store as rs


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    """每个用例独立库 + 重置模块状态。"""
    db = tmp_path / "codex_reasoning_test.db"
    # 重置模块全局
    rs._initialized = False
    rs._db_path = None
    rs._local = __import__("threading").local()
    rs._mem.clear()
    monkeypatch.setattr(rs, "_cfg", lambda: {"enabled": True, "dbPath": str(db),
                                             "ttlMinutes": 60, "memMaxEntries": 5,
                                             "memMaxBytes": 100000})
    rs.init()
    yield
    rs._mem.clear()


# ─────────────── normalize_items ───────────────

def test_normalize_reasoning_with_enc_kept():
    out = rs.normalize_items([
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAAAxyz"},
    ])
    assert len(out) == 1
    assert out[0]["type"] == "reasoning"
    assert out[0]["encrypted_content"] == "gAAAAxyz"
    assert "id" not in out[0]  # id 被剥


def test_normalize_bare_reasoning_dropped():
    out = rs.normalize_items([
        {"type": "reasoning", "id": "rs_1", "summary": []},  # 无 enc
    ])
    assert out == []


def test_normalize_empty_enc_dropped():
    out = rs.normalize_items([
        {"type": "reasoning", "encrypted_content": "   "},  # 空白 enc
    ])
    assert out == []


def test_normalize_enc_with_leading_space_dropped():
    # enc 必须 == strip()（对齐 CPA），带前后空格视为非法
    out = rs.normalize_items([
        {"type": "reasoning", "encrypted_content": " gAAAA"},
    ])
    assert out == []


def test_normalize_function_call_ok():
    out = rs.normalize_items([
        {"type": "function_call", "call_id": "call_a", "name": "exec", "arguments": "{}"},
    ])
    assert len(out) == 1
    assert out[0]["call_id"] == "call_a" and out[0]["name"] == "exec"


def test_normalize_function_call_missing_fields_dropped():
    assert rs.normalize_items([{"type": "function_call", "call_id": "", "name": "x", "arguments": "{}"}]) == []
    assert rs.normalize_items([{"type": "function_call", "call_id": "a", "name": "", "arguments": "{}"}]) == []
    # arguments 非字符串
    assert rs.normalize_items([{"type": "function_call", "call_id": "a", "name": "x", "arguments": {}}]) == []


def test_normalize_custom_tool_call():
    out = rs.normalize_items([
        {"type": "custom_tool_call", "call_id": "c1", "name": "t", "input": "data", "status": "completed"},
    ])
    assert len(out) == 1 and out[0]["type"] == "custom_tool_call"


def test_normalize_message_not_stored():
    out = rs.normalize_items([
        {"type": "message", "role": "assistant", "content": "hi"},
    ])
    assert out == []


def test_normalize_mixed_order_preserved():
    out = rs.normalize_items([
        {"type": "reasoning", "encrypted_content": "e1"},
        {"type": "message", "content": "x"},
        {"type": "function_call", "call_id": "a", "name": "exec", "arguments": "{}"},
    ])
    assert [x["type"] for x in out] == ["reasoning", "function_call"]


# ─────────────── LRU 二级缓存 ───────────────

def test_lru_entry_count_eviction():
    # memMaxEntries=5
    for i in range(7):
        rs.save_items(f"sk{i}", "m", [{"type": "reasoning", "encrypted_content": f"e{i}"}])
    # 内存最多 5 条，但 sqlite 全有
    assert len(rs._mem._d) <= 5
    # 被驱逐的仍能从 sqlite 取回
    got = rs.get_items("sk0", "m")
    assert got and got[0]["encrypted_content"] == "e0"


def test_lru_get_refreshes_recency():
    for i in range(5):
        rs.save_items(f"sk{i}", "m", [{"type": "reasoning", "encrypted_content": f"e{i}"}])
    rs.get_items("sk0", "m")  # 触碰 sk0
    rs.save_items("sk_new", "m", [{"type": "reasoning", "encrypted_content": "new"}])
    # sk0 刚被触碰，不应是最先驱逐的
    assert "m\x00sk0" in rs._mem._d


# ─────────────── sqlite 持久 ───────────────

def test_save_and_get():
    rs.save_items("skA", "gpt-5.5", [
        {"type": "reasoning", "encrypted_content": "enc1"},
        {"type": "function_call", "call_id": "c", "name": "exec", "arguments": "{}"},
    ])
    got = rs.get_items("skA", "gpt-5.5")
    assert [x["type"] for x in got] == ["reasoning", "function_call"]


def test_overwrite_same_session():
    rs.save_items("skB", "m", [{"type": "reasoning", "encrypted_content": "old"}])
    rs.save_items("skB", "m", [{"type": "reasoning", "encrypted_content": "new"}])
    got = rs.get_items("skB", "m")
    assert len(got) == 1 and got[0]["encrypted_content"] == "new"


def test_model_isolation():
    rs.save_items("skC", "m1", [{"type": "reasoning", "encrypted_content": "e1"}])
    rs.save_items("skC", "m2", [{"type": "reasoning", "encrypted_content": "e2"}])
    assert rs.get_items("skC", "m1")[0]["encrypted_content"] == "e1"
    assert rs.get_items("skC", "m2")[0]["encrypted_content"] == "e2"


def test_expired_not_returned(monkeypatch):
    rs.save_items("skD", "m", [{"type": "reasoning", "encrypted_content": "e"}])
    # 把 TTL 设为负，使其立即过期；清内存强制走 sqlite
    rs._mem.clear()
    monkeypatch.setattr(rs, "_ttl_seconds", lambda: 60)
    # 直接改库里 expires_at 到过去
    conn = rs._get_conn()
    conn.execute("UPDATE codex_reasoning SET expires_at=? WHERE session_key=?", (time.time() - 10, "skD"))
    conn.commit()
    assert rs.get_items("skD", "m") == []


# ─────────────── save_items 语义（关键回归）───────────────

def test_empty_norm_keeps_old_cache():
    """回归：agent 最后一轮纯文本（无 reasoning/tool）不能删掉整会话缓存。"""
    rs.save_items("skE", "m", [
        {"type": "reasoning", "encrypted_content": "keep_me"},
        {"type": "function_call", "call_id": "c", "name": "exec", "arguments": "{}"},
    ])
    # 下一轮只有 message → norm 空
    n = rs.save_items("skE", "m", [{"type": "message", "content": "总结完毕"}])
    assert n == 0
    # 旧缓存必须还在！
    got = rs.get_items("skE", "m")
    assert got and got[0]["encrypted_content"] == "keep_me", "空norm误删了旧缓存（bug回归）"


def test_disabled_returns_zero(monkeypatch):
    monkeypatch.setattr(rs, "_cfg", lambda: {"enabled": False})
    n = rs.save_items("skF", "m", [{"type": "reasoning", "encrypted_content": "e"}])
    assert n == 0


def test_empty_session_key_noop():
    assert rs.save_items("", "m", [{"type": "reasoning", "encrypted_content": "e"}]) == 0
    assert rs.get_items("", "m") == []


# ─────────────── invalidate 兜底 ───────────────

def test_invalidate_clears():
    rs.save_items("skG", "m", [{"type": "reasoning", "encrypted_content": "e"}])
    assert rs.get_items("skG", "m")
    rs.invalidate("skG", "m")
    assert rs.get_items("skG", "m") == []


# ─────────────── backfill 回填 ───────────────

def test_backfill_restores_deleted_reasoning():
    cached = [
        {"type": "reasoning", "encrypted_content": "enc1"},
        {"type": "function_call", "call_id": "call_a", "name": "exec", "arguments": "{}"},
    ]
    inp = [
        {"type": "message", "role": "user", "content": "go"},
        {"type": "function_call", "call_id": "call_a", "name": "exec", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_a", "output": "done"},
    ]
    new_inp, n = rs.backfill_input(inp, cached)
    assert n == 1  # 只补 reasoning（fc 已存在）
    types = [x["type"] for x in new_inp]
    assert "reasoning" in types
    # reasoning 在 output 之前
    assert types.index("reasoning") < types.index("function_call_output")


def test_backfill_fast_path_no_inject_when_downstream_honest():
    cached = [{"type": "reasoning", "encrypted_content": "enc1"}]
    inp = [
        {"type": "reasoning", "encrypted_content": "downstream_kept"},
        {"type": "function_call", "call_id": "a", "name": "exec", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "a", "output": "x"},
    ]
    new_inp, n = rs.backfill_input(inp, cached)
    assert n == 0  # 下游已带 reasoning，不补


def test_backfill_orphan_toolcall_not_injected():
    """缓存里的 function_call 在 input 里没有配对 output → 不补（避免孤儿）。"""
    cached = [
        {"type": "function_call", "call_id": "call_x", "name": "exec", "arguments": "{}"},
    ]
    inp = [
        {"type": "message", "role": "user", "content": "go"},
        # 没有 call_x 的 output
    ]
    new_inp, n = rs.backfill_input(inp, cached)
    assert n == 0


def test_backfill_toolcall_injected_when_output_present():
    cached = [
        {"type": "function_call", "call_id": "call_y", "name": "exec", "arguments": "{}"},
    ]
    inp = [
        {"type": "function_call_output", "call_id": "call_y", "output": "r"},
    ]
    new_inp, n = rs.backfill_input(inp, cached)
    assert n == 1
    assert new_inp[0]["type"] == "function_call"  # 插在 output 前


def test_backfill_callid_prefix_normalization():
    """call_id 带/不带 fc 前缀应能匹配（已存在则不重复补）。"""
    cached = [
        {"type": "function_call", "call_id": "fc_abc", "name": "exec", "arguments": "{}"},
    ]
    inp = [
        {"type": "function_call", "call_id": "call_abc", "name": "exec", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_abc", "output": "x"},
    ]
    new_inp, n = rs.backfill_input(inp, cached)
    assert n == 0  # fc_abc 与 call_abc 视为同一个，已存在不补


def test_backfill_empty_cache_noop():
    inp = [{"type": "message", "content": "x"}]
    new_inp, n = rs.backfill_input(inp, [])
    assert n == 0 and new_inp == inp


def test_backfill_pure_chat_not_polluted():
    """回归：纯对话（无任何工具调用）不补 reasoning，避免孤立块污染。"""
    cached = [{"type": "reasoning", "encrypted_content": "e"}]
    inp = [{"type": "message", "role": "user", "content": "你好"}]
    new_inp, n = rs.backfill_input(inp, cached)
    assert n == 0, "纯对话不应补 reasoning"
    assert new_inp == inp


def test_backfill_reasoning_only_with_toolcall():
    """reasoning 仅在 input 含工具调用且缺 reasoning 时补；插在 toolcall 链的 output 前。"""
    cached = [{"type": "reasoning", "encrypted_content": "e"}]
    inp = [
        {"type": "message", "role": "user", "content": "go"},
        {"type": "function_call", "call_id": "a", "name": "exec", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "a", "output": "r"},
    ]
    new_inp, n = rs.backfill_input(inp, cached)
    assert n == 1
    types = [x["type"] for x in new_inp]
    assert types.index("reasoning") < types.index("function_call_output")


# ─────────────── session_key ───────────────

def test_make_session_key_stable():
    k1 = rs.make_session_key("virus", "pck-123")
    k2 = rs.make_session_key("virus", "pck-123")
    assert k1 == k2 and k1


def test_make_session_key_empty_pck():
    assert rs.make_session_key("virus", "") == ""


def test_make_session_key_isolates_by_apikey():
    k1 = rs.make_session_key("user_a", "same-pck")
    k2 = rs.make_session_key("user_b", "same-pck")
    assert k1 != k2  # 不同 api_key 隔离
