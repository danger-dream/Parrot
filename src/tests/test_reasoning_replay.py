from __future__ import annotations

import base64

from src.openai import reasoning_replay as rr


def setup_function(_fn):
    rr.clear()


def _valid_encrypted_content(seed: int = 1) -> str:
    payload = bytearray(1 + 8 + 16 + 16 + 32)
    payload[0] = 0x80
    for i in range(9, len(payload)):
        payload[i] = (seed + i) % 256
    return base64.urlsafe_b64encode(bytes(payload)).decode("ascii").rstrip("=")


def test_scope_from_prompt_cache_key_and_metadata():
    assert rr.scope_from_payload("gpt-5.5", {"prompt_cache_key": "pck"}) == {
        "model": "gpt-5.5",
        "session_key": "prompt-cache:pck",
    }
    assert rr.scope_from_payload("gpt-5.5", {"metadata": {"user_id": '{"session_id":"s1"}'}}) == {
        "model": "gpt-5.5",
        "session_key": "claude:s1",
    }


def test_plain_metadata_user_id_is_not_a_replay_scope():
    assert rr.scope_from_payload("gpt-5.5", {"metadata": {"user_id": "user-123"}}) is None


def test_cache_normalizes_reasoning_and_tool_calls():
    encrypted_content = _valid_encrypted_content(3)
    ok = rr.cache_items("gpt-5.5", "prompt-cache:pck", [
        {"type": "message", "content": []},
        {"type": "reasoning", "id": "rs_1", "summary": [{"text": "skip"}], "encrypted_content": encrypted_content},
        {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "lookup", "arguments": "{}", "status": "completed"},
        {"type": "custom_tool_call", "call_id": "call_2", "name": "shell", "input": {"cmd": "ls"}},
    ])
    assert ok is True
    assert rr.get("gpt-5.5", "prompt-cache:pck") == [
        {"type": "reasoning", "summary": [], "content": None, "encrypted_content": encrypted_content},
        {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{}"},
        {"type": "custom_tool_call", "status": "completed", "call_id": "call_2", "name": "shell", "input": {"cmd": "ls"}},
    ]


def test_cache_rejects_invalid_encrypted_content():
    assert rr.cache_items("gpt-5.5", "prompt-cache:pck", [
        {"type": "reasoning", "encrypted_content": "bad"},
    ]) is False
    assert rr.get("gpt-5.5", "prompt-cache:pck") == []


def test_injects_reasoning_before_next_user_message():
    encrypted_content = _valid_encrypted_content(5)
    rr.cache_items("gpt-5.5", "prompt-cache:pck", [
        {"type": "reasoning", "encrypted_content": encrypted_content},
    ])
    payload = {"input": [{"type": "message", "role": "user", "content": "continue"}]}
    inserted = rr.inject_replay_items(payload, {"model": "gpt-5.5", "session_key": "prompt-cache:pck"})
    assert inserted == 1
    assert payload["input"][0] == {"type": "reasoning", "summary": [], "content": None, "encrypted_content": encrypted_content}


def test_injects_matching_function_call_before_tool_output_and_aligns_call_id():
    rr.cache_items("gpt-5.5", "prompt-cache:pck", [
        {"type": "function_call", "call_id": "fc_abc", "name": "lookup", "arguments": "{\"q\":1}"},
    ])
    payload = {"input": [
        {"type": "function_call_output", "call_id": "call_abc", "output": "ok"},
        {"type": "message", "role": "user", "content": "continue"},
    ]}
    inserted = rr.inject_replay_items(payload, {"model": "gpt-5.5", "session_key": "prompt-cache:pck"})
    assert inserted == 1
    assert payload["input"][0] == {
        "type": "function_call",
        "call_id": "call_abc",
        "name": "lookup",
        "arguments": "{\"q\":1}",
    }
    assert payload["input"][1]["type"] == "function_call_output"


def test_skips_tool_call_without_matching_output():
    rr.cache_items("gpt-5.5", "prompt-cache:pck", [
        {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{}"},
    ])
    payload = {"input": [{"type": "message", "role": "user", "content": "hi"}]}
    assert rr.inject_replay_items(payload, {"model": "gpt-5.5", "session_key": "prompt-cache:pck"}) == 0
    assert payload["input"] == [{"type": "message", "role": "user", "content": "hi"}]


def test_replay_cache_isolated_by_oauth_account_key():
    encrypted_content = _valid_encrypted_content(9)
    assert rr.cache_items(
        "gpt-5.5", "prompt-cache:pck", [
            {"type": "reasoning", "encrypted_content": encrypted_content},
        ], account_key="openai:account-a",
    ) is True

    account_b_payload = {
        "input": [{"type": "message", "role": "user", "content": "continue"}],
    }
    assert rr.inject_replay_items(account_b_payload, {
        "model": "gpt-5.5",
        "session_key": "prompt-cache:pck",
        "account_key": "openai:account-b",
    }) == 0

    account_a_payload = {
        "input": [{"type": "message", "role": "user", "content": "continue"}],
    }
    assert rr.inject_replay_items(account_a_payload, {
        "model": "gpt-5.5",
        "session_key": "prompt-cache:pck",
        "account_key": "openai:account-a",
    }) == 1
    assert account_a_payload["input"][0]["encrypted_content"] == encrypted_content


def test_delete_from_translator_ctx_clears_scope():
    rr.cache_items("gpt-5.5", "prompt-cache:pck", [{"type": "reasoning", "encrypted_content": _valid_encrypted_content(7)}])
    assert rr.delete_from_translator_ctx({"codex_reasoning_replay": {"model": "gpt-5.5", "session_key": "prompt-cache:pck"}}) is True
    assert rr.get("gpt-5.5", "prompt-cache:pck") == []
