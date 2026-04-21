"""会话亲和指纹。

核心设计（docs/06 §6.3）：同一会话在第 N 次请求到达与第 N-1 次请求完成
这两个时刻计算出的 hash 必然相等，据此把同一会话粘到同一渠道，避免
上游 prefix cache 失效。

查询（到达时）：去掉当前 user turn（messages[-1]），取剩下的最后两条 → hash
写入（完成时）：在 messages 末尾追加 assistant 回复，取最后两条 → hash

推导：
  N 次到达时 messages = [..., a_{N-1}, u_N]
    truncated = [..., a_{N-1}]   最后两条 = [u_{N-1}, a_{N-1}]
  N-1 次完成时 messages 曾是 [..., u_{N-1}]，加 a_{N-1} 后
    full = [..., u_{N-1}, a_{N-1}]  最后两条 = [u_{N-1}, a_{N-1}]
  两侧同形 → hash 相等。
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional


# 按 Anthropic Messages API 标准把每种 block 归一到稳定字段集合，
# 屏蔽两类来源的"噪声字段"，防止 hash 跑偏：
#   1) 客户端 cache_control ephemeral 标记随位置流动
#   2) 上游 SSE 中额外的非标字段（如 tool_use.caller={"type":"direct"}），
#      Claude Code 等客户端回发历史时会剔除，造成写入/查询两端不一致
_BLOCK_FIELDS: dict[str, tuple[str, ...]] = {
    "text":                  ("type", "text"),
    "thinking":              ("type", "thinking", "signature"),
    "redacted_thinking":     ("type", "data"),
    "tool_use":              ("type", "id", "name", "input"),
    "server_tool_use":       ("type", "id", "name", "input"),
    "mcp_tool_use":          ("type", "id", "name", "input", "server_name"),
    "tool_result":           ("type", "tool_use_id", "content", "is_error"),
    "mcp_tool_result":       ("type", "tool_use_id", "content", "is_error"),
    "image":                 ("type", "source"),
    "document":              ("type", "source", "title", "context", "citations"),
    "web_search_tool_result": ("type", "tool_use_id", "content"),
}

# message 顶层只保留稳定标识。上游回包上的 id / stop_reason / usage / model 等
# 都不应参与 fingerprint（客户端回发到历史里通常只带 role + content）。
_MSG_FIELDS: tuple[str, ...] = ("role", "content")


def _normalize_block(block):
    if not isinstance(block, dict):
        return block
    btype = block.get("type")
    wl = _BLOCK_FIELDS.get(btype)
    if wl is None:
        # 未知 block 类型：保底剥 cache_control，其它保留，避免误伤未来新类型
        return {k: v for k, v in block.items() if k != "cache_control"}
    # 对 content 字段递归（tool_result.content 可能嵌套 text/image block 列表）
    out = {}
    for k in wl:
        if k not in block:
            continue
        v = block[k]
        if k == "content" and isinstance(v, list):
            v = [_normalize_block(b) for b in v]
        out[k] = v
    return out


# thinking / redacted_thinking 是模型的中间推理，客户端回发到历史时策略不稳定
# （Claude Code 在某些场景直接丢弃），不应参与 fingerprint
_SKIP_BLOCK_TYPES = {"thinking", "redacted_thinking"}


def _normalize_msg(msg):
    """把一条 message 归一化为稳定的"可 hash"形状。"""
    if not isinstance(msg, dict):
        return msg
    out: dict = {}
    for k in _MSG_FIELDS:
        if k not in msg:
            continue
        v = msg[k]
        if k == "content":
            if isinstance(v, list):
                v = [
                    _normalize_block(b)
                    for b in v
                    if not (isinstance(b, dict) and b.get("type") in _SKIP_BLOCK_TYPES)
                ]
            # 字符串 content 保持原样
        out[k] = v
    return out


def _canon(msg_obj) -> str:
    """消息对象的 canonical JSON（稳定 key 排序 + 标准字段归一）。"""
    return json.dumps(
        _normalize_msg(msg_obj),
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )


def _make_hash(api_key_name: str, client_ip: str, msg_a, msg_b) -> str:
    raw = f"{api_key_name or ''}|{client_ip or ''}|{_canon(msg_a)}|{_canon(msg_b)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def fingerprint_query(api_key_name: str, client_ip: str, messages: list) -> Optional[str]:
    """请求到达时查询用。

    要求 messages 至少有 3 条（倒数第二条 + 第二条），否则返回 None
    （新会话无历史可锚定，跳过亲和）。
    """
    if not messages or len(messages) < 3:
        return None
    truncated = messages[:-1]
    last_two = truncated[-2:]
    return _make_hash(api_key_name, client_ip, last_two[0], last_two[1])


def fingerprint_write(api_key_name: str, client_ip: str,
                      messages: list, assistant_response: dict) -> Optional[str]:
    """响应完成时写入用。

    在 messages 末尾拼上本次产生的 assistant_response，取最后两条 hash。
    至少需要 2 条消息（当前请求 + assistant），少于则返回 None。
    """
    if not messages:
        return None
    full = list(messages)
    full.append(assistant_response)
    if len(full) < 2:
        return None
    last_two = full[-2:]
    return _make_hash(api_key_name, client_ip, last_two[0], last_two[1])


# ═══════════════════════════════════════════════════════════════
# OpenAI 家族（chat / responses）两套 fingerprint
# ═══════════════════════════════════════════════════════════════
#
# 命名空间前缀隔离 hash 空间：
#   - "openai-chat"   chat ingress（messages[] 形状）
#   - "openai-resp"   responses ingress（input items[] 形状）
#
# 与 anthropic 的 `fingerprint_query` / `fingerprint_write` 同理：Nth 请求
# 到达时 `query` 去掉当前 user turn 取倒数两条；(N-1) 完成时 `write` 追加
# assistant 回复取倒数两条 → 两端同形 → hash 相等。


# ─── Chat 归一化 ──────────────────────────────────────────────────

# chat message 顶层：仅保留稳定字段
_CHAT_MSG_FIELDS: tuple[str, ...] = ("role", "content", "tool_calls", "tool_call_id")

# parts 按类型白名单剥离
_CHAT_PART_FIELDS: dict[str, tuple[str, ...]] = {
    "text":        ("type", "text"),
    "image_url":   ("type", "image_url"),
    "input_audio": ("type", "input_audio"),
    "file":        ("type", "file"),
}


def _normalize_chat_part(part):
    if not isinstance(part, dict):
        return part
    ptype = part.get("type")
    wl = _CHAT_PART_FIELDS.get(ptype)
    if wl is None:
        return {k: v for k, v in part.items() if k != "cache_control"}
    return {k: part[k] for k in wl if k in part}


def _normalize_chat_tool_call(tc):
    if not isinstance(tc, dict):
        return tc
    out: dict = {}
    for k in ("id", "type"):
        if k in tc:
            out[k] = tc[k]
    fn = tc.get("function")
    if isinstance(fn, dict):
        out["function"] = {k: fn[k] for k in ("name", "arguments") if k in fn}
    return out


def _normalize_chat_msg(msg):
    if not isinstance(msg, dict):
        return msg
    out: dict = {}
    for k in _CHAT_MSG_FIELDS:
        if k not in msg:
            continue
        v = msg[k]
        if k == "content" and isinstance(v, list):
            v = [_normalize_chat_part(p) for p in v]
        elif k == "tool_calls" and isinstance(v, list):
            v = [_normalize_chat_tool_call(t) for t in v]
        out[k] = v
    return out


def _canon_chat(msg) -> str:
    return json.dumps(_normalize_chat_msg(msg),
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# ─── Responses 归一化 ────────────────────────────────────────────

_RESP_PART_FIELDS: dict[str, tuple[str, ...]] = {
    "input_text":   ("type", "text"),
    "output_text":  ("type", "text"),
    "input_image":  ("type", "image_url", "detail"),
    "input_file":   ("type", "file_id", "file_data", "filename"),
    "input_audio":  ("type", "input_audio"),
    "refusal":      ("type", "refusal"),
}


def _normalize_resp_part(p):
    if not isinstance(p, dict):
        return p
    ptype = p.get("type")
    wl = _RESP_PART_FIELDS.get(ptype)
    if wl is None:
        return {k: v for k, v in p.items() if k != "cache_control"}
    return {k: p[k] for k in wl if k in p}


def _normalize_resp_item(it):
    """把一个 Responses input item 归一化；返回 None 表示该 item 不稳定不参与 hash。"""
    if not isinstance(it, dict):
        return None
    t = it.get("type")
    if t == "message" or (t is None and "role" in it):
        # 兼容无 type 字段的裸 message（OpenCode / Codex CLI 等客户端）
        out: dict = {}
        for k in ("role", "content"):
            if k in it:
                out[k] = it[k]
        content = out.get("content")
        if isinstance(content, list):
            out["content"] = [_normalize_resp_part(p) for p in content]
        return out
    if t == "function_call":
        return {k: it[k] for k in ("type", "call_id", "name", "arguments") if k in it}
    if t == "function_call_output":
        return {k: it[k] for k in ("type", "call_id", "output") if k in it}
    # reasoning / built-in call items / item_reference 等：不参与 hash
    return None


def _canon_resp(item) -> str:
    n = _normalize_resp_item(item)
    if n is None:
        return ""
    return json.dumps(n, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# ─── 通用 hash（带 namespace 前缀） ──────────────────────────────


def _make_hash_canon(ns: str, api_key_name: str, client_ip: str,
                     a, b, *, canon) -> str:
    raw = f"{ns}|{api_key_name or ''}|{client_ip or ''}|{canon(a)}|{canon(b)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# ─── Chat 入口 ───────────────────────────────────────────────────


def fingerprint_query_chat(api_key_name: str, client_ip: str,
                            messages: list) -> Optional[str]:
    """Chat ingress 请求到达时查询用。要求至少 3 条消息。"""
    if not messages or len(messages) < 3:
        return None
    truncated = messages[:-1]
    last_two = truncated[-2:]
    return _make_hash_canon("openai-chat", api_key_name, client_ip,
                            last_two[0], last_two[1], canon=_canon_chat)


def fingerprint_write_chat(api_key_name: str, client_ip: str,
                            messages: list, assistant_response: dict) -> Optional[str]:
    """Chat ingress 响应完成时写入用。追加 assistant 回复后取倒数两条。"""
    if not messages:
        return None
    full = list(messages) + [assistant_response]
    if len(full) < 2:
        return None
    last_two = full[-2:]
    return _make_hash_canon("openai-chat", api_key_name, client_ip,
                            last_two[0], last_two[1], canon=_canon_chat)


# ─── Responses 入口 ──────────────────────────────────────────────


def _responses_relevant(items: list) -> list:
    """过滤掉 Responses input 中不稳定 item（reasoning / 内置工具 call 等）。

    注意：某些客户端（如 OpenCode / Codex CLI）发来的 message items 可能不带
    ``type`` 字段，只有 ``role`` + ``content``。这些同样是稳定 message，需要保留。
    """
    out: list = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        t = it.get("type")
        if t in ("message", "function_call", "function_call_output"):
            out.append(it)
        elif t is None and "role" in it:
            # 无 type 但有 role 的裸 message（兼容 OpenCode 等客户端）
            out.append(it)
    return out


def fingerprint_query_responses(api_key_name: str, client_ip: str,
                                 input_items: list) -> Optional[str]:
    """Responses ingress 请求到达时查询用。要求稳定 items 至少 3 条。"""
    rel = _responses_relevant(input_items)
    if len(rel) < 3:
        return None
    truncated = rel[:-1]
    last_two = truncated[-2:]
    return _make_hash_canon("openai-resp", api_key_name, client_ip,
                            last_two[0], last_two[1], canon=_canon_resp)


def fingerprint_write_responses(api_key_name: str, client_ip: str,
                                 input_items: list, output_items: list) -> Optional[str]:
    """Responses ingress 响应完成时写入用。

    把本次 input 中稳定 items 与本次 output 中 message / function_call items
    拼起来，取倒数两条 → hash。
    """
    rel = _responses_relevant(input_items)
    for it in (output_items or []):
        if isinstance(it, dict) and it.get("type") in ("message", "function_call"):
            rel.append(it)
    if len(rel) < 2:
        return None
    last_two = rel[-2:]
    return _make_hash_canon("openai-resp", api_key_name, client_ip,
                            last_two[0], last_two[1], canon=_canon_resp)
