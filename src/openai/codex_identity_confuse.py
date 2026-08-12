"""Codex Identity Confuse — 身份混淆层。

对发往 OpenAI Codex 上游的请求做身份字段混淆，防止上游通过指纹追踪多用户共享。

设计原则：
- Parrot 仍由 prompt_cache_key/session 亲和层决定本次请求的隔离 session key
- 发往上游的 Codex 身份字段统一改成隔离后的 session key / UUID5 派生值
- 混淆对象：installation-id、turn-metadata 内部的 turn_id/window_id/prompt_cache_key、
  x-codex-window-id header、thread-id header、x-client-request-id header
- 混淆算法：使用 OID namespace 的 SHA1-based UUID5
- 响应方向：把混淆值还原为原始值返回给客户端
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field


# UUID5 namespace：使用标准 OID namespace。
_NAMESPACE = uuid.NAMESPACE_OID


def _confuse_uuid(auth_id: str, kind: str, value: str) -> str:
    """生成混淆后的 UUID 字符串。

    算法：uuid5(NAMESPACE_OID, "parrot:codex:identity-confuse:{kind}:{auth_id}:{value}")。
    """
    if not auth_id or not value:
        return value
    name = f"parrot:codex:identity-confuse:{kind}:{auth_id.strip()}:{value.strip()}"
    return str(uuid.uuid5(_NAMESPACE, name))


@dataclass
class ConfuseReplacement:
    original: str
    confused: str


@dataclass
class ConfuseState:
    """混淆操作的上下文状态，用于响应还原。"""
    enabled: bool = False
    auth_id: str = ""
    # prompt_cache_key 混淆：原始值 ↔ Parrot 派生的隔离 session key
    original_prompt_cache_key: str = ""
    confused_prompt_cache_key: str = ""
    # turn_id 映射列表
    turn_ids: list[ConfuseReplacement] = field(default_factory=list)
    # window_id
    original_window_id: str = ""
    confused_window_id: str = ""
    # installation_id
    original_installation_id: str = ""
    confused_installation_id: str = ""

    def override_installation_for_upstream(self, installation_id: str) -> None:
        """Map an upstream account installation back only to a real downstream value."""
        if self.original_installation_id:
            self.confused_installation_id = installation_id

    def confuse_turn_id(self, turn_id: str) -> str:
        """混淆 turn_id，去重复映射。"""
        turn_id = turn_id.strip()
        if not self.enabled or not self.auth_id or not turn_id:
            return turn_id
        for r in self.turn_ids:
            if r.original == turn_id or r.confused == turn_id:
                return r.confused
        confused = _confuse_uuid(self.auth_id, "turn", turn_id)
        self.turn_ids.append(ConfuseReplacement(original=turn_id, confused=confused))
        return confused


def confuse_client_metadata(
    auth_id: str,
    client_metadata: dict | None,
    session_prompt_cache_key: str = "",
    *,
    state: ConfuseState | None = None,
    original_prompt_cache_key: str = "",
) -> tuple[dict, ConfuseState]:
    """混淆 client_metadata 中的身份字段。

    Args:
        auth_id: 用于混淆的身份标识（通常是 api_key_name）
        client_metadata: Codex WS frame 中的 client_metadata dict
        session_prompt_cache_key: Parrot 已派生的隔离 session key（用于替换
            turn-metadata 中的 prompt_cache_key / window_id）
        state: 可选既有状态。WS 长连接多轮 request 需要复用同一个状态，避免
            响应还原丢失上一帧注册过的 turn_id / installation 映射。
        original_prompt_cache_key: 顶层 prompt_cache_key 原始值。HTTP→WS 路径即使
            client_metadata 为空，也需要记录 raw↔session 映射，便于响应方向还原。

    Returns:
        (混淆后的 client_metadata, ConfuseState)
    """
    if not auth_id:
        return client_metadata or {}, ConfuseState()

    if state is not None and state.enabled:
        state.auth_id = state.auth_id or auth_id.strip()
    else:
        state = ConfuseState(enabled=True, auth_id=auth_id.strip())

    if original_prompt_cache_key and session_prompt_cache_key:
        state.original_prompt_cache_key = original_prompt_cache_key.strip()
        state.confused_prompt_cache_key = session_prompt_cache_key.strip()

    if not client_metadata:
        if state.original_prompt_cache_key or state.confused_prompt_cache_key:
            return {}, state
        return client_metadata or {}, ConfuseState()

    out = dict(client_metadata)

    # installation-id
    installation_id = (out.get("x-codex-installation-id") or "").strip()
    if installation_id:
        state.original_installation_id = installation_id
        state.confused_installation_id = _confuse_uuid(auth_id, "installation", installation_id)
        out["x-codex-installation-id"] = state.confused_installation_id

    # window-id
    window_id = (out.get("x-codex-window-id") or "").strip()
    if window_id and session_prompt_cache_key:
        state.original_window_id = window_id
        state.confused_window_id = f"{session_prompt_cache_key}:0"
        out["x-codex-window-id"] = state.confused_window_id

    # turn-metadata (JSON string inside client_metadata)
    turn_metadata_raw = (out.get("x-codex-turn-metadata") or "").strip()
    if turn_metadata_raw:
        out["x-codex-turn-metadata"] = _confuse_turn_metadata_str(
            turn_metadata_raw, state, session_prompt_cache_key)

    return out, state


def confuse_headers(headers: dict | None, state: ConfuseState,
                    session_prompt_cache_key: str = "") -> dict:
    """混淆 HTTP/WS headers 中的身份字段。

    Codex v0.135.0 WebSocket 只使用 `session-id` / `thread-id`（连字符）。
    废弃的 `session_id`（下划线）和 `conversation_id` 始终删除。
    """
    out = dict(headers or {})
    if not state.enabled:
        return _delete_deprecated_headers(out)

    pck = state.confused_prompt_cache_key or session_prompt_cache_key or ""

    if pck:
        _set_header_case_insensitive(out, "session-id", pck)
        _set_header_case_insensitive(out, "thread-id", pck)
        _set_header_case_insensitive(out, "x-client-request-id", pck)
        _set_header_case_insensitive(out, "x-codex-window-id", state.confused_window_id or f"{pck}:0")
    elif state.confused_window_id and _header_value_case_insensitive(headers, "x-codex-window-id"):
        _set_header_case_insensitive(out, "x-codex-window-id", state.confused_window_id)

    # x-codex-turn-metadata (header)
    turn_metadata_raw = _header_value_case_insensitive(out, "x-codex-turn-metadata")
    if turn_metadata_raw:
        _set_header_case_insensitive(
            out,
            "x-codex-turn-metadata",
            _confuse_turn_metadata_str(turn_metadata_raw, state, session_prompt_cache_key),
        )

    return _delete_deprecated_headers(out)


def confuse_response_payload(data: bytes, state: ConfuseState) -> bytes:
    """把响应里的原始身份值替换成混淆值。

    记录或审计上游响应时，先把原始身份值替换成请求期混淆值，
    不暴露原始 prompt_cache_key / turn_id。Parrot 下游发送前再调用 expose 还原。
    """
    if not state.enabled or not data:
        return data

    # 先替换更具体的 window_id，避免 prompt_cache_key 是 window_id 前缀时丢失完整映射。
    data = _replace_bytes(data, state.original_window_id, state.confused_window_id)
    data = _replace_bytes(data, state.original_prompt_cache_key, state.confused_prompt_cache_key)
    for r in state.turn_ids:
        data = _replace_bytes(data, r.original, r.confused)
    data = _replace_bytes(data, state.original_installation_id, state.confused_installation_id)
    return data


def expose_response_payload(data: bytes, state: ConfuseState) -> bytes:
    """还原上游响应中的混淆值为原始值，返回给下游客户端。"""
    if not state.enabled or not data:
        return data

    # 先还原更具体的 window_id，避免 prompt_cache_key 是 window_id 前缀时半截替换。
    data = _replace_bytes(data, state.confused_window_id, state.original_window_id)
    data = _replace_bytes(data, state.confused_prompt_cache_key, state.original_prompt_cache_key)

    for r in state.turn_ids:
        data = _replace_bytes(data, r.confused, r.original)

    data = _replace_bytes(data, state.confused_installation_id, state.original_installation_id)
    return data


# ─── 内部辅助 ────────────────────────────────────────────────────


def _confuse_turn_metadata_str(raw: str, state: ConfuseState,
                               session_prompt_cache_key: str) -> str:
    """混淆 turn-metadata JSON 字符串内的身份字段。"""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw

    if not isinstance(obj, dict):
        return raw

    # prompt_cache_key inside turn-metadata → 用 session 的隔离值替换
    pck = (obj.get("prompt_cache_key") or "").strip()
    if pck and session_prompt_cache_key:
        state.original_prompt_cache_key = pck
        state.confused_prompt_cache_key = session_prompt_cache_key
        obj["prompt_cache_key"] = session_prompt_cache_key
    elif session_prompt_cache_key and state.original_prompt_cache_key:
        state.confused_prompt_cache_key = session_prompt_cache_key

    # turn_id
    turn_id = (obj.get("turn_id") or "").strip()
    if turn_id:
        obj["turn_id"] = state.confuse_turn_id(turn_id)

    # window_id inside turn-metadata
    window_id = (obj.get("window_id") or "").strip()
    if window_id and session_prompt_cache_key:
        if not state.original_window_id:
            state.original_window_id = window_id
        state.confused_window_id = f"{session_prompt_cache_key}:0"
        obj["window_id"] = state.confused_window_id

    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _replace_bytes(data: bytes, from_val: str, to_val: str) -> bytes:
    """对非空且不同的身份值执行确定性字节级替换。"""
    if not from_val or not to_val or from_val == to_val:
        return data
    from_bytes = from_val.encode("utf-8")
    if from_bytes not in data:
        return data
    return data.replace(from_bytes, to_val.encode("utf-8"))


def _header_value_case_insensitive(headers: dict | None, key: str) -> str:
    if not headers or not key:
        return ""
    for k, v in headers.items():
        if str(k).lower() == key.lower():
            return str(v).strip()
    return ""


def _set_header_case_insensitive(headers: dict, key: str, value: str) -> None:
    if not key or value is None:
        return
    for k in list(headers.keys()):
        if str(k).lower() == key.lower():
            del headers[k]
    headers[key] = str(value)


def _delete_deprecated_headers(headers: dict) -> dict:
    """Remove legacy session_id (underscore) and conversation_id variants."""
    for key in list(headers.keys()):
        if str(key).lower() in ("session_id", "conversation_id", "conversation-id"):
            del headers[key]
    return headers
