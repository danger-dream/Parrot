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


def _collect_cache_controls(payload: dict) -> list[dict]:
    controls = [
        tool["cache_control"]
        for tool in payload.get("tools") or []
        if isinstance(tool.get("cache_control"), dict)
    ]
    for block in payload.get("system") or []:
        if isinstance(block, dict) and isinstance(block.get("cache_control"), dict):
            controls.append(block["cache_control"])
    for message in payload.get("messages") or []:
        for block in message.get("content") or []:
            if isinstance(block, dict) and isinstance(block.get("cache_control"), dict):
                controls.append(block["cache_control"])
    return controls


def _bridged_anthropic_payload() -> dict:
    return {
        "model": "claude-fable-5-1",
        "system": [{"type": "text", "text": "stable expensive instructions"}],
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "bootstrap"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ack"}]},
            {"role": "user", "content": [{"type": "text", "text": "dynamic tail"}]},
        ],
    }


def test_generated_breakpoints_match_five_minute_top_level_cache_control():
    """A bare prompt_cache_key yields a 5m umbrella; blocks must not claim 1h.

    Anthropic rejects the request when the top-level control and the block it
    targets disagree ("they must have matching TTLs").
    """
    payload = _bridged_anthropic_payload()
    cache_hints.apply_openai_cache_to_anthropic_payload(
        {"prompt_cache_key": "conversation-1"}, payload,
    )

    assert payload["cache_control"] == {"type": "ephemeral"}
    controls = _collect_cache_controls(payload)
    assert controls
    assert all("ttl" not in control for control in controls)


def test_generated_breakpoints_match_one_hour_top_level_cache_control():
    payload = _bridged_anthropic_payload()
    cache_hints.apply_openai_cache_to_anthropic_payload(
        {"prompt_cache_key": "conversation-1", "prompt_cache_retention": "24h"}, payload,
    )

    assert payload["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    controls = _collect_cache_controls(payload)
    assert controls
    assert all(control.get("ttl") == "1h" for control in controls)


def test_generated_breakpoints_keep_one_hour_without_top_level_control():
    payload = _bridged_anthropic_payload()
    cache_hints.apply_anthropic_block_cache_breakpoints(payload)

    assert "cache_control" not in payload
    controls = _collect_cache_controls(payload)
    assert controls
    assert all(control.get("ttl") == "1h" for control in controls)


def _five_turn_anthropic_payload() -> dict:
    """A payload long enough to reach the second-to-last-user breakpoint slot."""
    return {
        "model": "claude-fable-5-1",
        "system": [{"type": "text", "text": "stable expensive instructions"}],
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "bootstrap"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ack"}]},
            {"role": "user", "content": [{"type": "text", "text": "follow-up"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ack"}]},
            {"role": "user", "content": [{"type": "text", "text": "dynamic tail"}]},
        ],
    }


def test_top_level_control_reserves_one_of_the_four_block_slots():
    """The umbrella counts against Anthropic's four-block allowance.

    A bridged OpenAI payload used to add four block breakpoints on top of the
    generated top-level control, so the request was rejected with "A maximum of
    4 blocks with cache_control may be provided. Found 5."
    """
    payload = _five_turn_anthropic_payload()
    cache_hints.apply_openai_cache_to_anthropic_payload(
        {"prompt_cache_key": "conversation-1"}, payload,
    )

    assert payload["cache_control"] == {"type": "ephemeral"}
    blocks = _collect_cache_controls(payload)
    # Three block breakpoints plus the umbrella stays within the limit of four,
    # so the second-to-last user turn is left without a breakpoint.
    assert len(blocks) == 3
    assert len(blocks) + 1 <= 4


def test_block_breakpoints_still_fill_four_slots_without_a_top_level_control():
    """Without an umbrella the four block slots remain fully available."""
    payload = _five_turn_anthropic_payload()
    cache_hints.apply_anthropic_block_cache_breakpoints(payload)

    assert "cache_control" not in payload
    blocks = _collect_cache_controls(payload)
    assert len(blocks) == 4


def test_header_derived_internal_session_hint_is_supported():
    body = {
        "_parrot_claude_code_session_id": "57f87dc1-34b4-4d8b-acf1-4526c8ebb6e8",
        "messages": [{"role": "user", "content": "dynamic only"}],
    }

    key = cache_hints.stable_prompt_cache_key_from_anthropic(
        body, model="gpt-5.5", api_key_name="cc-switch", client_ip="203.0.113.8",
    )

    assert key.startswith("parrot:cache:v1:a2o-session:")
