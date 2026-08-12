"""Codex Identity Confuse 单元测试。

覆盖：
- UUID 混淆算法的确定性和命名空间
- client_metadata 混淆
- headers 混淆
- turn-metadata JSON 内部字段混淆
- 响应还原（expose）
- 边界情况（空值、缺字段、disabled state）
- conversation_id 删除
- prompt_cache_key 不动（顶层）
"""

from __future__ import annotations

import json
import os as _ap_os
import sys as _ap_sys
import uuid

_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation

_isolation.isolate()

from src.openai.codex_identity_confuse import (
    ConfuseState,
    ConfuseReplacement,
    _confuse_uuid,
    confuse_client_metadata,
    confuse_headers,
    expose_response_payload,
    _confuse_turn_metadata_str,
    _replace_bytes,
)


# ─── _confuse_uuid ────────────────────────────────────────────────


class TestConfuseUUID:
    """测试 UUID 混淆算法。"""

    def test_deterministic(self):
        """同一组输入始终生成相同 UUID。"""
        a = _confuse_uuid("user-123", "installation", "inst-abc")
        b = _confuse_uuid("user-123", "installation", "inst-abc")
        assert a == b

    def test_different_auth_different_result(self):
        """不同 auth_id 产生不同混淆值。"""
        a = _confuse_uuid("user-1", "installation", "inst-abc")
        b = _confuse_uuid("user-2", "installation", "inst-abc")
        assert a != b

    def test_different_kind_different_result(self):
        """不同 kind 产生不同混淆值。"""
        a = _confuse_uuid("user-1", "installation", "val")
        b = _confuse_uuid("user-1", "turn", "val")
        assert a != b

    def test_valid_uuid_format(self):
        """输出是合法 UUID 格式。"""
        result = _confuse_uuid("auth", "kind", "value")
        parsed = uuid.UUID(result)
        assert parsed.version == 5

    def test_empty_auth_passthrough(self):
        """auth_id 为空时原样返回。"""
        assert _confuse_uuid("", "kind", "val") == "val"

    def test_empty_value_passthrough(self):
        """value 为空时原样返回。"""
        assert _confuse_uuid("auth", "kind", "") == ""

    def test_parrot_namespace_contract(self):
        """Parrot 命名空间必须稳定地参与 UUID5 派生。"""
        name = "parrot:codex:identity-confuse:installation:test-auth:test-install"
        expected = str(uuid.uuid5(uuid.NAMESPACE_OID, name))
        actual = _confuse_uuid("test-auth", "installation", "test-install")
        assert actual == expected


# ─── confuse_client_metadata ──────────────────────────────────────


class TestConfuseClientMetadata:
    """测试 client_metadata 混淆。"""

    def test_installation_id_confused(self):
        meta = {"x-codex-installation-id": "real-install-id-123"}
        out, state = confuse_client_metadata("api-key-1", meta)
        assert out["x-codex-installation-id"] != "real-install-id-123"
        assert state.original_installation_id == "real-install-id-123"
        assert state.confused_installation_id == out["x-codex-installation-id"]

    def test_window_id_confused_with_session_pck(self):
        meta = {"x-codex-window-id": "thread-123:0"}
        out, state = confuse_client_metadata("key1", meta, session_prompt_cache_key="iso-pck-456")
        assert out["x-codex-window-id"] == "iso-pck-456:0"

    def test_window_id_unchanged_without_session_pck(self):
        meta = {"x-codex-window-id": "thread-123:0"}
        out, state = confuse_client_metadata("key1", meta, session_prompt_cache_key="")
        assert out["x-codex-window-id"] == "thread-123:0"

    def test_turn_metadata_confused(self):
        tm = json.dumps({
            "prompt_cache_key": "orig-pck",
            "turn_id": "turn-aaa",
            "window_id": "thread-123:0",
        })
        meta = {"x-codex-turn-metadata": tm}
        out, state = confuse_client_metadata("key1", meta, session_prompt_cache_key="iso-pck")
        parsed = json.loads(out["x-codex-turn-metadata"])
        assert parsed["prompt_cache_key"] == "iso-pck"
        assert parsed["turn_id"] != "turn-aaa"
        assert parsed["window_id"] == "iso-pck:0"
        assert state.original_prompt_cache_key == "orig-pck"
        assert state.confused_prompt_cache_key == "iso-pck"
        assert len(state.turn_ids) == 1
        assert state.turn_ids[0].original == "turn-aaa"

    def test_empty_metadata_returns_empty(self):
        out, state = confuse_client_metadata("key1", {})
        assert out == {}
        assert not state.enabled

    def test_no_auth_id_passthrough(self):
        meta = {"x-codex-installation-id": "abc"}
        out, state = confuse_client_metadata("", meta)
        assert out["x-codex-installation-id"] == "abc"
        assert not state.enabled

    def test_none_metadata(self):
        out, state = confuse_client_metadata("key1", None)
        assert out == {}
        assert not state.enabled

    def test_unrelated_fields_preserved(self):
        meta = {"x-codex-installation-id": "inst", "x-codex-beta-features": "feat1"}
        out, state = confuse_client_metadata("key1", meta)
        assert out["x-codex-beta-features"] == "feat1"
        assert out["x-codex-installation-id"] != "inst"


# ─── confuse_headers ──────────────────────────────────────────────


class TestConfuseHeaders:
    """测试 header 混淆。"""

    def test_thread_id_confused(self):
        state = ConfuseState(enabled=True, auth_id="key1",
                             confused_prompt_cache_key="confused-pck")
        headers = {"thread-id": "original-thread", "session-id": "raw-session"}
        out = confuse_headers(headers, state)
        assert out["thread-id"] == "confused-pck"
        # 官方 Codex WS 使用 session-id/thread-id，同样不能泄漏原始身份锚点。
        assert out["session-id"] == "confused-pck"

    def test_x_client_request_id_confused(self):
        state = ConfuseState(enabled=True, auth_id="key1",
                             confused_prompt_cache_key="confused-pck")
        headers = {"x-client-request-id": "original-req"}
        out = confuse_headers(headers, state)
        assert out["x-client-request-id"] == "confused-pck"

    def test_x_client_request_id_confused_even_if_empty(self):
        """即使 x-client-request-id 为空字符串，只要 key 存在就应被替换。"""
        state = ConfuseState(enabled=True, auth_id="key1",
                             confused_prompt_cache_key="confused-pck")
        headers = {"x-client-request-id": ""}
        out = confuse_headers(headers, state)
        assert out["x-client-request-id"] == "confused-pck"

    def test_window_id_header_confused(self):
        state = ConfuseState(enabled=True, auth_id="key1",
                             confused_window_id="confused-pck:0")
        headers = {"x-codex-window-id": "thread-123:0"}
        out = confuse_headers(headers, state)
        assert out["x-codex-window-id"] == "confused-pck:0"

    def test_conversation_id_deleted(self):
        state = ConfuseState(enabled=True, auth_id="key1")
        headers = {"conversation_id": "old-conv", "other": "keep"}
        out = confuse_headers(headers, state)
        assert "conversation_id" not in out
        assert out["other"] == "keep"

    def test_conversation_id_case_insensitive_delete(self):
        state = ConfuseState(enabled=True, auth_id="key1")
        headers = {"Conversation_id": "v1", "Conversation-Id": "v2"}
        out = confuse_headers(headers, state)
        assert "Conversation_id" not in out
        assert "Conversation-Id" not in out

    def test_turn_metadata_header_confused(self):
        state = ConfuseState(enabled=True, auth_id="key1")
        tm = json.dumps({"turn_id": "tid-1", "prompt_cache_key": "pck-orig"})
        headers = {"x-codex-turn-metadata": tm}
        out = confuse_headers(headers, state, session_prompt_cache_key="iso-pck")
        parsed = json.loads(out["x-codex-turn-metadata"])
        assert parsed["prompt_cache_key"] == "iso-pck"
        assert parsed["turn_id"] != "tid-1"

    def test_disabled_state_still_drops_deprecated_conversation_id(self):
        state = ConfuseState(enabled=False)
        headers = {"thread-id": "keep", "conversation_id": "old"}
        out = confuse_headers(headers, state)
        assert out == {"thread-id": "keep"}

    def test_missing_fields_no_crash(self):
        state = ConfuseState(enabled=True, auth_id="key1",
                             confused_prompt_cache_key="pck")
        headers = {}  # 空 headers
        out = confuse_headers(headers, state)
        assert out["session-id"] == "pck"
        assert out["thread-id"] == "pck"
        assert out["x-client-request-id"] == "pck"
        assert out["x-codex-window-id"] == "pck:0"

    def test_uses_session_pck_fallback(self):
        """当 state 没有 confused_prompt_cache_key 但有 session_prompt_cache_key 时"""
        state = ConfuseState(enabled=True, auth_id="key1")
        headers = {"x-client-request-id": "orig", "thread-id": "orig"}
        out = confuse_headers(headers, state, session_prompt_cache_key="fallback-pck")
        assert out["x-client-request-id"] == "fallback-pck"
        assert out["thread-id"] == "fallback-pck"

    def test_header_case_insensitive_overwrites_without_duplicates(self):
        state = ConfuseState(enabled=True, auth_id="key1",
                             confused_prompt_cache_key="pck")
        headers = {"Thread-Id": "raw-thread", "User-Agent": "ua", "Conversation-Id": "old"}
        out = confuse_headers(headers, state)
        lowered = {k.lower(): v for k, v in out.items()}
        assert lowered["thread-id"] == "pck"
        assert "conversation-id" not in lowered
        assert sum(1 for k in out if k.lower() == "thread-id") == 1

    def test_empty_client_metadata_can_record_prompt_cache_mapping(self):
        state = ConfuseState(enabled=True, auth_id="key1")
        out, state = confuse_client_metadata(
            "key1", {}, session_prompt_cache_key="iso-pck", state=state,
            original_prompt_cache_key="raw-pck",
        )
        assert out == {}
        assert state.original_prompt_cache_key == "raw-pck"
        assert state.confused_prompt_cache_key == "iso-pck"
        restored = expose_response_payload(b'{"prompt_cache_key":"iso-pck"}', state)
        assert b'raw-pck' in restored

    def test_ws_state_reuse_preserves_previous_turn_mapping(self):
        first_meta = {"x-codex-turn-metadata": json.dumps({"turn_id": "turn-1", "prompt_cache_key": "raw"})}
        _, state = confuse_client_metadata("key1", first_meta, session_prompt_cache_key="iso")
        second_meta = {"x-codex-turn-metadata": json.dumps({"turn_id": "turn-2", "prompt_cache_key": "raw"})}
        _, state = confuse_client_metadata("key1", second_meta, session_prompt_cache_key="iso", state=state)
        assert len(state.turn_ids) == 2
        payload = json.dumps({"a": state.turn_ids[0].confused, "b": state.turn_ids[1].confused}).encode()
        restored = expose_response_payload(payload, state)
        assert b'turn-1' in restored and b'turn-2' in restored


# ─── ConfuseState.confuse_turn_id ─────────────────────────────────


class TestConfuseTurnId:

    def test_same_turn_id_reuses_mapping(self):
        state = ConfuseState(enabled=True, auth_id="key1")
        first = state.confuse_turn_id("tid-1")
        second = state.confuse_turn_id("tid-1")
        assert first == second
        assert len(state.turn_ids) == 1

    def test_different_turn_ids_get_different_mappings(self):
        state = ConfuseState(enabled=True, auth_id="key1")
        a = state.confuse_turn_id("tid-1")
        b = state.confuse_turn_id("tid-2")
        assert a != b
        assert len(state.turn_ids) == 2

    def test_disabled_passthrough(self):
        state = ConfuseState(enabled=False)
        assert state.confuse_turn_id("tid-1") == "tid-1"

    def test_empty_passthrough(self):
        state = ConfuseState(enabled=True, auth_id="key1")
        assert state.confuse_turn_id("") == ""


# ─── expose_response_payload ──────────────────────────────────────


class TestExposeResponsePayload:

    def test_prompt_cache_key_restored(self):
        state = ConfuseState(
            enabled=True, auth_id="key1",
            original_prompt_cache_key="orig-pck",
            confused_prompt_cache_key="confused-pck",
        )
        data = b'{"prompt_cache_key":"confused-pck","other":"confused-pck-suffix"}'
        result = expose_response_payload(data, state)
        assert b"orig-pck" in result
        assert b"confused-pck" not in result

    def test_turn_id_restored(self):
        state = ConfuseState(
            enabled=True, auth_id="key1",
            turn_ids=[ConfuseReplacement(original="tid-orig", confused="tid-confused")],
        )
        data = b'{"turn_id":"tid-confused"}'
        result = expose_response_payload(data, state)
        assert b"tid-orig" in result
        assert b"tid-confused" not in result

    def test_installation_id_restored(self):
        state = ConfuseState(
            enabled=True, auth_id="key1",
            original_installation_id="inst-orig",
            confused_installation_id="inst-confused",
        )
        data = b'"x-codex-installation-id":"inst-confused"'
        result = expose_response_payload(data, state)
        assert b"inst-orig" in result

    def test_window_id_restored(self):
        state = ConfuseState(
            enabled=True, auth_id="key1",
            original_window_id="thread-123:0",
            confused_window_id="iso-pck:0",
        )
        data = b'"window_id":"iso-pck:0"'
        result = expose_response_payload(data, state)
        assert b"thread-123:0" in result

    def test_disabled_passthrough(self):
        state = ConfuseState(enabled=False)
        data = b'unchanged'
        assert expose_response_payload(data, state) == data

    def test_empty_data(self):
        state = ConfuseState(enabled=True, auth_id="key1")
        assert expose_response_payload(b"", state) == b""

    def test_multiple_replacements(self):
        state = ConfuseState(
            enabled=True, auth_id="key1",
            original_prompt_cache_key="orig-pck",
            confused_prompt_cache_key="c-pck",
            turn_ids=[
                ConfuseReplacement(original="tid-1", confused="c-tid-1"),
                ConfuseReplacement(original="tid-2", confused="c-tid-2"),
            ],
        )
        data = b'first c-pck then c-tid-1 and c-tid-2 done'
        result = expose_response_payload(data, state)
        assert b"orig-pck" in result
        assert b"tid-1" in result
        assert b"tid-2" in result
        assert b"c-pck" not in result
        assert b"c-tid-1" not in result
        assert b"c-tid-2" not in result


# ─── _replace_bytes ───────────────────────────────────────────────


class TestReplaceBytes:

    def test_normal_replace(self):
        assert _replace_bytes(b"hello world", "world", "earth") == b"hello earth"

    def test_no_match_unchanged(self):
        assert _replace_bytes(b"hello", "xyz", "abc") == b"hello"

    def test_empty_from_unchanged(self):
        assert _replace_bytes(b"hello", "", "abc") == b"hello"

    def test_same_from_to_unchanged(self):
        assert _replace_bytes(b"hello", "hello", "hello") == b"hello"

    def test_multiple_occurrences(self):
        assert _replace_bytes(b"aXbXc", "X", "Y") == b"aYbYc"


# ─── _confuse_turn_metadata_str ───────────────────────────────────


class TestConfuseTurnMetadataStr:

    def test_all_fields_confused(self):
        state = ConfuseState(enabled=True, auth_id="key1")
        raw = json.dumps({
            "prompt_cache_key": "pck-orig",
            "turn_id": "tid-orig",
            "window_id": "wid-orig",
            "other_field": "keep",
        })
        result = _confuse_turn_metadata_str(raw, state, session_prompt_cache_key="iso-pck")
        parsed = json.loads(result)
        assert parsed["prompt_cache_key"] == "iso-pck"
        assert parsed["turn_id"] != "tid-orig"
        assert parsed["window_id"] == "iso-pck:0"
        assert parsed["other_field"] == "keep"

    def test_invalid_json_passthrough(self):
        state = ConfuseState(enabled=True, auth_id="key1")
        raw = "not-json"
        assert _confuse_turn_metadata_str(raw, state, "pck") == "not-json"

    def test_partial_fields(self):
        state = ConfuseState(enabled=True, auth_id="key1")
        raw = json.dumps({"turn_id": "tid-1"})
        result = _confuse_turn_metadata_str(raw, state, session_prompt_cache_key="")
        parsed = json.loads(result)
        assert parsed["turn_id"] != "tid-1"
        # no prompt_cache_key or window_id changes
        assert "prompt_cache_key" not in parsed

    def test_empty_session_pck_no_pck_change(self):
        state = ConfuseState(enabled=True, auth_id="key1")
        raw = json.dumps({"prompt_cache_key": "orig"})
        result = _confuse_turn_metadata_str(raw, state, session_prompt_cache_key="")
        parsed = json.loads(result)
        assert parsed["prompt_cache_key"] == "orig"  # 不改



# ─── 端到端集成测试 ────────────────────────────────────────────────


class TestEndToEndConfuseAndExpose:
    """模拟完整的请求混淆→上游处理→响应还原流程。"""

    def test_full_round_trip(self):
        """模拟 Codex WS 完整流程：
        1. 下游发送含身份信息的 client_metadata
        2. 混淆后发给上游
        3. 上游响应中包含混淆后的值
        4. expose 还原后返回原始值给下游
        """
        auth_id = "api-key-alice"
        session_pck = "iso-session-abc123"

        # Step 1: 下游 client_metadata
        client_meta = {
            "x-codex-installation-id": "real-install-uuid-999",
            "x-codex-window-id": "real-thread:2",
            "x-codex-turn-metadata": json.dumps({
                "prompt_cache_key": "real-pck-xyz",
                "turn_id": "real-turn-001",
                "window_id": "real-thread:2",
                "some_other": "data",
            }),
        }

        # Step 2: 混淆
        confused_meta, state = confuse_client_metadata(
            auth_id, client_meta, session_prompt_cache_key=session_pck)

        # 验证混淆结果
        assert confused_meta["x-codex-installation-id"] != "real-install-uuid-999"
        assert confused_meta["x-codex-window-id"] == f"{session_pck}:0"
        tm = json.loads(confused_meta["x-codex-turn-metadata"])
        assert tm["prompt_cache_key"] == session_pck
        assert tm["turn_id"] != "real-turn-001"
        assert tm["window_id"] == f"{session_pck}:0"
        assert tm["some_other"] == "data"  # 无关字段不动

        confused_turn_id = tm["turn_id"]
        confused_install_id = confused_meta["x-codex-installation-id"]

        # Step 3: 模拟上游响应（含混淆值）
        upstream_response = json.dumps({
            "type": "response.completed",
            "response": {
                "id": "resp_123",
                "turn_id": confused_turn_id,
                "prompt_cache_key": session_pck,
                "metadata": {
                    "installation": confused_install_id,
                },
            },
        }).encode("utf-8")

        # Step 4: 还原
        restored = expose_response_payload(upstream_response, state)
        restored_obj = json.loads(restored)

        # 验证还原结果
        assert restored_obj["response"]["turn_id"] == "real-turn-001"
        assert restored_obj["response"]["prompt_cache_key"] == "real-pck-xyz"
        assert restored_obj["response"]["metadata"]["installation"] == "real-install-uuid-999"

    def test_headers_and_metadata_use_same_state(self):
        """验证 header 混淆和 client_metadata 混淆使用一致的状态。"""
        auth_id = "key-bob"
        session_pck = "iso-pck-bob"

        # 先混淆 client_metadata 获取 state
        client_meta = {
            "x-codex-installation-id": "bob-install",
            "x-codex-turn-metadata": json.dumps({
                "turn_id": "bob-turn-1",
                "prompt_cache_key": "bob-pck",
            }),
        }
        _, state = confuse_client_metadata(auth_id, client_meta,
                                           session_prompt_cache_key=session_pck)

        # 用同一个 state 混淆 headers
        headers = {
            "thread-id": "bob-thread",
            "x-client-request-id": "bob-req",
            "x-codex-window-id": "bob-thread:0",
            "session-id": "keep-this-session",
        }
        confused_headers = confuse_headers(headers, state,
                                           session_prompt_cache_key=session_pck)

        # session-id / thread-id / x-client-request-id 均使用隔离 session_pck
        assert confused_headers["session-id"] == session_pck
        assert confused_headers["thread-id"] == session_pck
        assert confused_headers["x-client-request-id"] == session_pck
        # x-codex-window-id
        assert confused_headers["x-codex-window-id"] == f"{session_pck}:0"

    def test_non_oauth_channel_no_confuse_except_deprecated_header_drop(self):
        """非 OAuth 通道不做身份混淆，但 deprecated conversation_id 仍会被删除。"""
        state = ConfuseState()  # enabled=False
        meta = {"x-codex-installation-id": "keep"}
        out, _ = confuse_client_metadata("", meta)
        assert out["x-codex-installation-id"] == "keep"

        headers = {"thread-id": "keep", "conversation_id": "old"}
        out_h = confuse_headers(headers, state)
        assert out_h["thread-id"] == "keep"
        assert "conversation_id" not in out_h
