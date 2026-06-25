from __future__ import annotations

from src import cache_hints


def _anthropic_body(session_id: str | None, *, assistant_text: str = "ack") -> dict:
    body = {
        "model": "gpt-5.5",
        "system": [{"type": "text", "text": "stable expensive instructions", "cache_control": {"type": "ephemeral"}}],
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "user", "content": "bootstrap"},
            {"role": "assistant", "content": [{"type": "text", "text": assistant_text}]},
            {"role": "user", "content": "dynamic tail"},
        ],
    }
    if session_id is not None:
        body["metadata"] = {"user_id": f'{{"device_id":"dev-1","session_id":"{session_id}"}}'}
    return body


def test_anthropic_session_cache_key_uses_metadata_session_id():
    body1 = _anthropic_body("session-abc", assistant_text="old assistant")
    body2 = _anthropic_body("session-abc", assistant_text="changed growing history")

    key1 = cache_hints.stable_prompt_cache_key_from_anthropic(
        body1, model="gpt-5.5", api_key_name="cc-switch", client_ip="203.0.113.8",
    )
    key2 = cache_hints.stable_prompt_cache_key_from_anthropic(
        body2, model="gpt-5.5", api_key_name="cc-switch", client_ip="203.0.113.8",
    )

    assert key1 == key2
    assert key1.startswith("parrot:cache:v1:a2o-session:")


def test_anthropic_session_cache_key_isolated_by_session_and_client():
    base = _anthropic_body("session-abc")
    other_session = _anthropic_body("session-def")

    key1 = cache_hints.stable_prompt_cache_key_from_anthropic(
        base, model="gpt-5.5", api_key_name="cc-switch", client_ip="203.0.113.8",
    )
    key2 = cache_hints.stable_prompt_cache_key_from_anthropic(
        other_session, model="gpt-5.5", api_key_name="cc-switch", client_ip="203.0.113.8",
    )
    key3 = cache_hints.stable_prompt_cache_key_from_anthropic(
        base, model="gpt-5.5", api_key_name="other-key", client_ip="203.0.113.8",
    )
    key4 = cache_hints.stable_prompt_cache_key_from_anthropic(
        base, model="gpt-5.5", api_key_name="cc-switch", client_ip="198.51.100.9",
    )

    assert len({key1, key2, key3, key4}) == 4


def test_anthropic_cache_key_falls_back_to_prefix_without_session_id():
    body = _anthropic_body(None)

    key = cache_hints.stable_prompt_cache_key_from_anthropic(
        body, model="gpt-5.5", api_key_name="cc-switch", client_ip="203.0.113.8",
    )

    assert key.startswith("parrot:cache:v1:a2o:")


def test_plain_metadata_user_id_is_not_treated_as_session_id():
    body = {
        "metadata": {"user_id": "user-123"},
        "messages": [{"role": "user", "content": "dynamic only"}],
    }

    assert cache_hints.anthropic_session_id(body) is None
    assert cache_hints.stable_prompt_cache_key_from_anthropic(body) is None


def test_header_derived_internal_session_hint_is_supported():
    body = {
        "_parrot_claude_code_session_id": "57f87dc1-34b4-4d8b-acf1-4526c8ebb6e8",
        "messages": [{"role": "user", "content": "dynamic only"}],
    }

    key = cache_hints.stable_prompt_cache_key_from_anthropic(
        body, model="gpt-5.5", api_key_name="cc-switch", client_ip="203.0.113.8",
    )

    assert key.startswith("parrot:cache:v1:a2o-session:")
