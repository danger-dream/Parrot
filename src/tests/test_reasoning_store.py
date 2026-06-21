"""Codex encrypted_content 透明透传 + 最小 replay cache 契约测试。

Parrot 不解密、不伪造 encrypted_content；只在有稳定 session anchor 时缓存
上游最终 Responses output 里的最小 replay items，并在下轮注入。
"""

from __future__ import annotations

import base64

import src.openai.transform.codex_oauth_transform as t


def _valid_encrypted_content(seed: int = 1) -> str:
    payload = bytearray(1 + 8 + 16 + 16 + 32)
    payload[0] = 0x80
    for i in range(9, len(payload)):
        payload[i] = (seed + i) % 256
    return base64.urlsafe_b64encode(bytes(payload)).decode("ascii").rstrip("=")


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
    enc = _valid_encrypted_content(23)
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


def test_reasoning_replay_module_is_runtime_contract():
    import src.openai.reasoning_replay as rr

    rr.clear()
    enc = _valid_encrypted_content(29)
    assert rr.cache_items(
        "gpt-5.5",
        "prompt-cache:pck",
        [{"type": "reasoning", "encrypted_content": enc}],
    ) is True
    assert rr.get("gpt-5.5", "prompt-cache:pck") == [{
        "type": "reasoning",
        "summary": [],
        "content": None,
        "encrypted_content": enc,
    }]
    rr.clear()


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


def test_invalid_encrypted_content_clears_replay_scope():
    import src.failover as f
    import src.openai.reasoning_replay as rr

    rr.clear()
    rr.cache_items("gpt-5.5", "prompt-cache:pck", [{"type": "reasoning", "encrypted_content": _valid_encrypted_content(31)}])
    assert rr.get("gpt-5.5", "prompt-cache:pck")
    assert f._maybe_clear_codex_reasoning_replay({
        "codex_reasoning_replay": {"model": "gpt-5.5", "session_key": "prompt-cache:pck"},
    }) is True
    assert rr.get("gpt-5.5", "prompt-cache:pck") == []


def test_context_length_exceeded_is_request_invalid_not_channel_failure():
    import src.failover as f

    cases = [
        (
            "OpenAI JSON",
            '{"type":"invalid_request_error","code":"context_length_exceeded",'
            '"message":"Your input exceeds the context window of this model. Please adjust your input and try again."}',
            503,
            400,
        ),
        (
            "Anthropic request_too_large",
            '{"type":"error","error":{"type":"request_too_large",'
            '"message":"Request size exceeds model context window"}}',
            413,
            413,
        ),
        (
            "provider wording",
            "prompt is too long: 200001 tokens > 200000 maximum",
            None,
            400,
        ),
    ]

    for name, detail, input_status, expected_status in cases:
        result = f.AttemptResult(
            outcome="upstream_error_json",
            error_detail=detail,
            http_status=input_status,
        )
        out = f._request_invalid_result_if_needed(result)
        assert out.outcome == "request_invalid", name
        assert out.http_status == expected_status, name
        assert not out.stream_started, name
        assert not f._should_cooldown(out.outcome), name


def test_context_length_detector_does_not_swallow_tpm_rate_limits():
    import src.failover as f

    cases = [
        "HTTP 413: tokens per minute exceeded for this project",
        "rate_limit_error: input token rate limit exceeded, please retry later",
        "Request failed: RPM quota exceeded for this organization",
    ]
    for detail in cases:
        result = f.AttemptResult(
            outcome="http_error",
            error_detail=detail,
            http_status=413,
        )
        out = f._request_invalid_result_if_needed(result)
        assert out.outcome == "http_error", detail
        assert out.http_status == 413, detail


def test_request_invalid_413_keeps_request_too_large_for_anthropic_ingress():
    import json
    import src.errors as errors
    import src.failover as f

    resp = f._json_error_for_ingress(
        "anthropic",
        413,
        errors.classify_http_status(413),
        "Request size exceeds model context window",
    )
    body = json.loads(resp.body)
    assert body["error"]["type"] == "request_too_large"
    assert resp.status_code == 413

    openai_resp = f._json_error_for_ingress(
        "responses",
        413,
        errors.classify_http_status(413),
        "Request size exceeds model context window",
    )
    openai_body = json.loads(openai_resp.body)
    assert openai_body["error"]["type"] == "invalid_request_error"
    assert openai_resp.status_code == 413


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
