from __future__ import annotations

import asyncio
import copy
import json

import pytest

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


def test_anthropic_fallback_key_stays_stable_as_tool_history_grows():
    body = _anthropic_body(None)
    first_call = {"role": "assistant", "content": [{
        "type": "tool_use", "id": "call-first", "name": "lookup", "input": {"q": "first"},
    }]}
    body["messages"] = [body["messages"][0], first_call, {
        "role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-first", "content": "first result"}],
    }]
    original = copy.deepcopy(body)
    kwargs = {"model": "gpt-6-astra", "api_key_name": "tenant", "client_ip": "203.0.113.8"}
    key = cache_hints.stable_prompt_cache_key_from_anthropic(body, **kwargs)

    for index in range(1, 5):
        body["messages"].extend([
            {"role": "assistant", "content": [{"type": "tool_use", "id": f"call-{index}", "name": "lookup", "input": {"q": index}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"call-{index}", "content": f"result {index}"}]},
        ])
        source = copy.deepcopy(body)
        payload = {}
        cache_hints.apply_anthropic_cache_to_openai_payload(body, payload, **kwargs)
        assert payload["prompt_cache_key"] == key
        assert body == source
    assert body["messages"][:3] == original["messages"]


def test_anthropic_fallback_key_ignores_later_user_assistant_turns():
    body = _anthropic_body(None)
    key = cache_hints.stable_prompt_cache_key_from_anthropic(body)
    body["messages"].extend([
        {"role": "assistant", "content": "later answer"},
        {"role": "user", "content": "new user question"},
    ])
    assert cache_hints.stable_prompt_cache_key_from_anthropic(body) == key


def test_anthropic_fallback_key_does_not_merge_shared_bootstrap_conversations():
    first = _anthropic_body(None, assistant_text="first conversation reply")
    other = _anthropic_body(None, assistant_text="second conversation reply")
    assert cache_hints.stable_prompt_cache_key_from_anthropic(first) != (
        cache_hints.stable_prompt_cache_key_from_anthropic(other)
    )
    other = copy.deepcopy(first)
    other["messages"][0]["content"] = "different initial user"
    assert cache_hints.stable_prompt_cache_key_from_anthropic(first) != (
        cache_hints.stable_prompt_cache_key_from_anthropic(other)
    )


def test_anthropic_fallback_key_isolates_tasks_after_shared_bootstrap_exchange():
    first = _anthropic_body(None)
    other = copy.deepcopy(first)
    first["messages"][2]["content"] = "first real task"
    other["messages"][2]["content"] = "different real task"
    first_key = cache_hints.stable_prompt_cache_key_from_anthropic(first)
    other_key = cache_hints.stable_prompt_cache_key_from_anthropic(other)
    assert first_key != other_key
    first["messages"].extend([
        {"role": "assistant", "content": "later answer"},
        {"role": "user", "content": "later follow-up"},
    ])
    assert cache_hints.stable_prompt_cache_key_from_anthropic(first) == first_key


def test_anthropic_fallback_key_keeps_model_tenant_and_ip_isolation():
    body = _anthropic_body(None)
    common = {"model": "gpt-6-astra", "api_key_name": "tenant", "client_ip": "203.0.113.8"}
    keys = {cache_hints.stable_prompt_cache_key_from_anthropic(body, **common)}
    for field, value in (("model", "gpt-6-sol"), ("api_key_name", "other-tenant"), ("client_ip", "198.51.100.9")):
        keys.add(cache_hints.stable_prompt_cache_key_from_anthropic(body, **{**common, field: value}))
    assert len(keys) == 4


def test_anthropic_fallback_key_ignores_cache_control_but_keeps_static_prefix():
    body = _anthropic_body(None)
    key = cache_hints.stable_prompt_cache_key_from_anthropic(body)
    with_hints = copy.deepcopy(body)
    with_hints["system"][0]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    with_hints["tools"][0]["cache_control"] = {"type": "ephemeral"}
    assert cache_hints.stable_prompt_cache_key_from_anthropic(with_hints) == key
    changed = copy.deepcopy(body)
    changed["system"][0]["text"] = "different instructions"
    assert cache_hints.stable_prompt_cache_key_from_anthropic(changed) != key
    changed = copy.deepcopy(body)
    changed["tools"][0]["name"] = "different_tool"
    assert cache_hints.stable_prompt_cache_key_from_anthropic(changed) != key


def test_anthropic_cache_mapping_preserves_explicit_target_key():
    payload = {"prompt_cache_key": "explicit-target-key"}
    cache_hints.apply_anthropic_cache_to_openai_payload(_anthropic_body(None), payload)
    assert payload["prompt_cache_key"] == "explicit-target-key"


@pytest.mark.parametrize("text,expected", [
    ("x-anthropic-billing-header: cch=11111;", ""),
    ("x-anthropic-billing-header: cch=11111;\nStable prompt", "Stable prompt"),
    ("x-anthropic-billing-header: cch=11111;\n\nStable prompt", "Stable prompt"),
    ("x-anthropic-billing-header: cch=11111;\r\n\r\nStable prompt", "Stable prompt"),
    ("x-anthropic-billing-header: cch=11111;\r\rStable prompt", "Stable prompt"),
    ("Keep this literal:\nx-anthropic-billing-header: example",
     "Keep this literal:\nx-anthropic-billing-header: example"),
    ("<total_tokens>15000000 tokens left</total_tokens>",
     "<total_tokens>15000000 tokens left</total_tokens>"),
    ("Stable prompt\n\n", "Stable prompt\n\n"),
])
def test_strip_billing_header_preserves_prompt_text(text, expected):
    assert cache_hints.strip_leading_anthropic_billing_header(text) == expected


@pytest.mark.parametrize("protocol", ["openai-chat", "openai-responses"])
@pytest.mark.parametrize("system_shape", ["string", "inline", "block"])
@pytest.mark.parametrize("separator", ["\n", "\r\n"])
def test_anthropic_billing_header_keeps_final_cache_key_and_prompt(protocol, system_shape, separator):
    from src.openai.channel.api_channel import OpenAIApiChannel

    channel = OpenAIApiChannel({
        "name": "cache-test", "baseUrl": "https://upstream.invalid",
        "protocol": protocol, "providerId": "openai",
    })
    body = _anthropic_body(None)
    if system_shape == "string":
        body["system"] = "stable expensive instructions"
    original = copy.deepcopy(body)
    payloads = []
    for cch in (None, "11111", "22222"):
        source = copy.deepcopy(original)
        if cch is not None:
            header = f"x-anthropic-billing-header: cc_version=2.1.282; cch={cch};"
            if system_shape == "string":
                source["system"] = header + separator * 2 + source["system"]
            elif system_shape == "inline":
                source["system"][0]["text"] = header + separator * 2 + source["system"][0]["text"]
            else:
                source["system"].insert(0, {"type": "text", "text": header})
        before = copy.deepcopy(source)
        request = asyncio.run(channel.build_upstream_request(source, "gpt-6-astra", ingress_protocol="anthropic"))
        assert source == before
        payloads.append(json.loads(request.body))
    assert body == original
    assert payloads[0]["prompt_cache_key"]
    assert payloads == [payloads[0]] * 3
    prompt = payloads[0]["messages"][0]["content"] if protocol == "openai-chat" else payloads[0]["instructions"]
    assert prompt == "stable expensive instructions"


def test_native_anthropic_channel_preserves_billing_header_and_budget():
    from src.channel.api_channel import ApiChannel

    channel = ApiChannel({
        "name": "native-cache-test", "baseUrl": "https://upstream.invalid",
        "protocol": "anthropic", "cc_mimicry": False,
    })
    body = _anthropic_body(None)
    body["system"].insert(0, {"type": "text", "text": "x-anthropic-billing-header: cch=11111;"})
    body["system"][1]["text"] += "\n<total_tokens>15000000 tokens left</total_tokens>"
    original = copy.deepcopy(body)
    request = asyncio.run(channel.build_upstream_request(body, "claude-sonnet-4-6", ingress_protocol="anthropic"))
    assert json.loads(request.body)["system"] == original["system"]
    assert body == original


def test_anthropic_cache_key_preserves_literal_billing_header_and_budget():
    body = _anthropic_body(None)
    key = cache_hints.stable_prompt_cache_key_from_anthropic(body)
    body["system"][0]["text"] += "\nKeep this literal:\nx-anthropic-billing-header: example"
    literal_key = cache_hints.stable_prompt_cache_key_from_anthropic(body)
    assert literal_key != key
    body["system"][0]["text"] += "\n<total_tokens>15000000 tokens left</total_tokens>"
    assert cache_hints.stable_prompt_cache_key_from_anthropic(body) != literal_key


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


def _anthropic_body_with_1h_system():
    return {
        "system": [{
            "type": "text", "text": "stable instructions",
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }],
        "messages": [{"role": "user", "content": "hi"}],
    }


@pytest.mark.parametrize("model", ["gpt-5.6-sol", "gpt-6-astra", "gpt-6.1-sol"])
def test_gpt_56_plus_keeps_default_cache_lifetime_for_anthropic_1h(model):
    body = _anthropic_body_with_1h_system()
    payload: dict = {}

    cache_hints.apply_anthropic_cache_to_openai_payload(body, payload, model=model)

    assert "prompt_cache_retention" not in payload
    assert "prompt_cache_options" not in payload
    assert payload["prompt_cache_key"].startswith("parrot:cache:v1:a2o:")


@pytest.mark.parametrize("model", ["gpt-5.5", "gpt-5.4", "gpt-5-mini", "gpt-4.1"])
def test_pre_56_models_keep_24h_retention_for_anthropic_1h(model):
    body = _anthropic_body_with_1h_system()

    assert cache_hints.openai_retention_from_anthropic(body, model=model) == "24h"
