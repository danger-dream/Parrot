"""Codex encrypted_content 透明透传契约测试。

v0.21.0 曾引入 Parrot 本地 reasoning replay store/backfill，但 encrypted_content
是上游签名/加密的 opaque transcript 状态，代理无法可靠维护。现在 Parrot 的
责任边界收缩为：请求 include、保留下游提供的 encrypted_content、剥 store=false
不适用的 reasoning id，但绝不本地缓存/补全/修复 EC。
"""

from __future__ import annotations

import importlib

import src.openai.transform.codex_oauth_transform as t


def _transform(body: dict) -> dict:
    return t.apply_codex_oauth_transform(body, resolved_model="gpt-5.5")


def test_transform_injects_reasoning_encrypted_content_include():
    out = _transform({
        "model": "gpt-5.5",
        "input": [{"type": "message", "role": "user", "content": "hi"}],
    })
    assert "reasoning.encrypted_content" in (out.get("include") or [])


def test_transform_does_not_duplicate_include():
    out = _transform({
        "model": "gpt-5.5",
        "include": ["reasoning.encrypted_content"],
        "input": [{"type": "message", "role": "user", "content": "hi"}],
    })
    assert (out.get("include") or []).count("reasoning.encrypted_content") == 1


def test_reasoning_with_encrypted_content_is_transparently_preserved_without_id():
    enc = "gAAAAopaque-valid-looking-blob"
    out = _transform({
        "model": "gpt-5.5",
        "input": [
            {"type": "message", "role": "user", "content": "hi"},
            {"type": "reasoning", "id": "rs_123", "summary": [], "content": None,
             "encrypted_content": enc},
        ],
    })
    reasoning = [it for it in out["input"] if it.get("type") == "reasoning"]
    assert len(reasoning) == 1
    assert reasoning[0]["encrypted_content"] == enc
    assert "id" not in reasoning[0]
    assert reasoning[0].get("summary") == []


def test_bare_reasoning_without_encrypted_content_is_dropped():
    out = _transform({
        "model": "gpt-5.5",
        "input": [
            {"type": "message", "role": "user", "content": "hi"},
            {"type": "reasoning", "id": "rs_123", "summary": []},
        ],
    })
    assert [it.get("type") for it in out["input"]] == ["message"]


def test_session_key_argument_is_ignored_no_backfill_happens():
    out = t.apply_codex_oauth_transform({
        "model": "gpt-5.5",
        "input": [
            {"type": "function_call_output", "call_id": "call_1", "output": "{}"},
            {"type": "message", "role": "user", "content": "continue"},
        ],
    }, resolved_model="gpt-5.5", session_key="legacy-session")
    assert all(it.get("type") != "reasoning" for it in out["input"])


def test_reasoning_store_module_removed_from_runtime_contract():
    try:
        importlib.import_module("src.openai.reasoning_store")
    except ModuleNotFoundError:
        return
    raise AssertionError("src.openai.reasoning_store should not be part of runtime contract")


def test_invalid_encrypted_content_is_request_invalid_not_channel_failure():
    import src.failover as f

    result = f.AttemptResult(
        outcome="stream_upstream_error",
        error_detail=(
            "invalid_encrypted_content: The encrypted content gAAA...tDk= could not be verified. "
            "Reason: Encrypted content could not be decrypted or parsed."
        ),
        http_status=503,
    )
    out = f._request_invalid_result_if_needed(result)
    assert out.outcome == "request_invalid"
    assert out.http_status == 400
    assert not f._should_cooldown(out.outcome)


def test_non_encrypted_upstream_error_still_channel_failure():
    import src.failover as f

    result = f.AttemptResult(
        outcome="stream_upstream_error",
        error_detail="upstream overloaded",
        http_status=503,
    )
    out = f._request_invalid_result_if_needed(result)
    assert out.outcome == "stream_upstream_error"
    assert out.http_status == 503
    assert f._should_cooldown(out.outcome)


def test_retry_body_without_encrypted_content_strips_input_only_keeps_include():
    import src.failover as f

    body = {
        "model": "gpt-5.5",
        "include": ["reasoning.encrypted_content"],
        "input": [
            {"type": "message", "role": "user", "content": "hi"},
            {"type": "reasoning", "id": "rs_1", "encrypted_content": "bad", "summary": []},
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": [
                    {"type": "encrypted_content", "encrypted_content": "bad-tool"},
                    {"type": "input_text", "text": "visible"},
                ],
            },
        ],
    }
    retry_body, removed = f._retry_body_without_encrypted_content(body)
    assert removed == 2
    assert retry_body["include"] == ["reasoning.encrypted_content"]
    assert [it["type"] for it in retry_body["input"]] == ["message", "function_call_output"]
    assert retry_body["input"][1]["output"] == [{"type": "input_text", "text": "visible"}]
    # 原 body 不被就地修改
    assert any(it.get("type") == "reasoning" for it in body["input"])


def test_retry_body_without_encrypted_content_noop_when_no_input_ec():
    import src.failover as f

    body = {"model": "gpt-5.5", "input": [{"type": "message", "role": "user", "content": "hi"}]}
    retry_body, removed = f._retry_body_without_encrypted_content(body)
    assert removed == 0
    assert retry_body == body
    assert retry_body is not body
