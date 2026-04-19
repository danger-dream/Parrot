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


def _canon(msg_obj) -> str:
    """消息对象的 canonical JSON（稳定 key 排序）。"""
    return json.dumps(msg_obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


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
