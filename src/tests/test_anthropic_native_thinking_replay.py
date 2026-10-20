"""Signed history travels with messages; missing history must not block tools."""
from __future__ import annotations

import copy
import importlib
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import config
from src.channel.api_channel import ApiChannel
from src.openai import anthropic_reasoning_replay as replay, store
from src.openai.transform import responses_to_anthropic as bridge
from src.protocols.runtime import apply_non_stream_response_translator, make_stream_translator, retry_body_without_encrypted_content
from src.transform import cc_mimicry

MODEL = "claude-sonnet-4-6"
OWNER = {"api_key_name": "test-owner", "channel_key": "api:replay", "model": MODEL}
USER = {"role": "user", "content": "lookup"}
RESULT = {"type": "function_call_output", "call_id": "call_1", "output": "7"}


@pytest.fixture(autouse=True)
def passthrough(monkeypatch):
    monkeypatch.setitem(config.get().setdefault("openai", {}), "reasoningBridge", "passthrough")


def message():
    return {"id": "msg_source", "role": "assistant", "model": MODEL, "stop_reason": "tool_use",
            "content": [
                {"type": "thinking", "thinking": "  original\ntext  ", "signature": "signature-one"},
                {"type": "redacted_thinking", "data": "opaque-redacted"},
                {"type": "thinking", "thinking": "", "signature": "signature-only"},
                {"type": "text", "text": "visible"},
                {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {}},
            ], "usage": {"input_tokens": 5, "output_tokens": 9}}


def output():
    return bridge.translate_response(message(), **OWNER)["output"]


def request(items, **overrides):
    body = {"model": MODEL, "reasoning": {"effort": "low"}, "input": [USER, *items, RESULT]}
    options = {"api_key_name": OWNER["api_key_name"], "target_model": MODEL}
    options.update(overrides)
    return bridge.translate_request(body, **options)


def wire(*events):
    return b"".join(("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n").encode() for e in events)


def stream_events():
    events = [{"type": "message_start", "message": {"model": "actual-model-version", "usage": {"input_tokens": 5}}}]
    for index, block in enumerate(message()["content"]):
        block = copy.deepcopy(block)
        signature = block.pop("signature", None)
        events.append({"type": "content_block_start", "index": index, "content_block": block})
        if signature:
            for part in (signature[:4], signature[4:]):
                events.append({"type": "content_block_delta", "index": index, "delta": {"type": "signature_delta", "signature": part}})
        events.append({"type": "content_block_stop", "index": index})
    return events + [{"type": "message_delta", "delta": {"stop_reason": "tool_use"}}, {"type": "message_stop"}]


def assert_fallback(sent):
    assert sent["thinking"] == {"type": "adaptive"}
    assert all(b["type"] not in ("thinking", "redacted_thinking") for m in sent["messages"] for b in m["content"])
    assistant = next(m for m in sent["messages"] if m["role"] == "assistant")
    assert assistant["content"] == message()["content"][3:]
    assert sent["messages"][-1]["content"] == [{"type": "tool_result", "tool_use_id": "call_1", "content": "7"}]


@pytest.mark.parametrize("mode", ["passthrough", "drop"])
def test_nonstream_stateless_original_blocks_roundtrip(mode, monkeypatch):
    monkeypatch.setitem(config.get()["openai"], "reasoningBridge", mode)
    raw = message()
    original = copy.deepcopy(raw)
    out = apply_non_stream_response_translator(raw, {"response_translator": "responses_to_anthropic", "model_for_response": MODEL, **OWNER})
    assert raw == original
    if mode == "drop":
        assert [i["type"] for i in out["output"]] == ["message", "function_call"]
        assert "original" not in json.dumps(out)
        assert "encrypted_content" not in json.dumps(out)
        assert_fallback(request(out["output"]))
    else:
        assert [i["type"] for i in out["output"]] == ["reasoning", "reasoning", "reasoning", "message", "function_call"]
        assert out["output"][0]["encrypted_content"] == "claude#signature-one"
        assert out["output"][1]["encrypted_content"] == "claude-redacted-thinking:opaque-redacted"
        assert request(out["output"])["messages"][1]["content"] == original["content"]


@pytest.mark.parametrize("mode", ["passthrough", "drop"])
@pytest.mark.parametrize("bytewise", [False, True])
def test_stream_signature_fragments_redaction_and_empty_thinking_roundtrip(mode, bytewise, monkeypatch):
    monkeypatch.setitem(config.get()["openai"], "reasoningBridge", mode)
    tr = make_stream_translator({"response_translator": "responses_to_anthropic", "model_for_response": MODEL, **OWNER})
    raw = wire(*stream_events())
    parts = (bytes([b]) for b in raw) if bytewise else [raw]
    chunks = [chunk for part in parts for chunk in tr.feed(part)] + list(tr.close())
    events = [json.loads(line[6:]) for line in b"".join(chunks).decode().splitlines() if line.startswith("data: ")]
    out = events[-1]["response"]
    assert events[-1]["type"] == "response.completed"
    assert [e["item"] for e in events if e["type"] == "response.output_item.done"] == out["output"]
    if mode == "drop":
        assert not any("reasoning_summary" in e["type"] for e in events)
        assert [i["type"] for i in out["output"]] == ["message", "function_call"]
        assert_fallback(request(out["output"]))
    else:
        assert [i["type"] for i in out["output"]] == ["reasoning", "reasoning", "reasoning", "message", "function_call"]
        assert request(out["output"])["messages"][1]["content"] == message()["content"]


def test_previous_response_id_replays_native_blocks_after_module_reload():
    store.init()
    out = bridge.translate_response(message(), **OWNER, current_input_items=[USER])
    importlib.reload(replay)
    sent = bridge.translate_request({"model": MODEL, "reasoning": {"effort": "low"},
        "previous_response_id": out["id"], "input": [RESULT]},
        api_key_name=OWNER["api_key_name"], target_model=MODEL)
    assert sent["messages"][1]["content"] == message()["content"]


@pytest.mark.parametrize("change", ["missing_signature", "unknown_ciphertext", "drop_all", "bare_id"])
def test_missing_native_history_downgrades_without_losing_tool_results(change):
    items = output()
    if change == "missing_signature":
        items[0].pop("encrypted_content")
    elif change == "unknown_ciphertext":
        items[0]["encrypted_content"] = "foreign-openai-ciphertext"
    elif change == "bare_id":
        items[0] = {"type": "reasoning", "id": items[0]["id"]}
    else:
        items = items[3:]
    original = copy.deepcopy(items)
    assert_fallback(request(items))
    assert items == original


@pytest.mark.parametrize("field,value", [("api_key_name", "other-owner"), ("target_model", "claude-opus-4-6")])
def test_client_supplied_native_history_is_not_bound_to_local_owner(field, value):
    assert request(output(), **{field: value})["messages"][1]["content"] == message()["content"]


def test_restart_and_item_id_changes_do_not_lose_signed_history():
    items = json.loads(json.dumps(output()))
    for item in items:
        item.pop("id", None)
    importlib.reload(replay)
    assert request(items)["messages"][1]["content"] == message()["content"]


def test_older_unreplayable_turn_does_not_block_valid_latest_tool_turn():
    earlier = [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "old"}]},
               {"role": "assistant", "content": "old answer"}, {"role": "user", "content": "next"}]
    sent = request([*earlier, *output()])
    assert sent["messages"][-2]["content"] == message()["content"]
    assert sent["thinking"]["type"] != "disabled"


def test_incomplete_signature_block_has_no_replay_carrier():
    tr = make_stream_translator({"response_translator": "responses_to_anthropic", "model_for_response": MODEL, **OWNER})
    chunks = list(tr.feed(wire(*stream_events()[:3]))) + list(tr.close())
    final = json.loads(b"".join(chunks).decode().split("data: ")[-1])
    assert final["type"] == "response.failed"
    assert "encrypted_content" not in json.dumps(final)


@pytest.mark.parametrize("mimicry", [False, True])
@pytest.mark.parametrize("missing", [False, True])
async def test_api_channel_replays_or_downgrades_in_final_serialized_body(mimicry, missing):
    channel = ApiChannel({"name": "replay", "baseUrl": "https://api.anthropic.com", "apiKey": "fixture",
        "protocol": "anthropic", "cc_mimicry": mimicry, "models": [{"alias": "alias", "real": MODEL}]})
    items = output()[3:] if missing else output()
    up = await channel.build_upstream_request({"model": "alias", "_api_key_name": OWNER["api_key_name"],
        "input": [USER, *items, RESULT], "reasoning": {"effort": "low"}}, MODEL, ingress_protocol="responses")
    sent = json.loads(up.body)
    assistant = next(m for m in sent["messages"] if m["role"] == "assistant")
    if missing:
        assert sent["thinking"] == {"type": "adaptive"}
        assert [b["type"] for b in assistant["content"]] == ["text", "tool_use"]
        assert sent["messages"][-1]["content"][0]["tool_use_id"] == "call_1"
    else:
        assert assistant["content"][:3] == message()["content"][:3]


async def test_mimicry_default_adaptive_thinking_survives_missing_history():
    channel = ApiChannel({"name": "replay", "baseUrl": "https://api.anthropic.com", "apiKey": "fixture",
        "protocol": "anthropic", "cc_mimicry": True, "models": [{"alias": "alias", "real": MODEL}]})
    up = await channel.build_upstream_request({"model": "alias", "input": [USER, *output()[3:], RESULT]}, MODEL, ingress_protocol="responses")
    assert json.loads(up.body)["thinking"]["type"] == "adaptive"


@pytest.mark.parametrize("active_tool_turn", [True, False])
def test_mimicry_replays_all_signed_thinking_verbatim(active_tool_turn):
    old = {"role": "assistant", "content": [{"type": "thinking", "thinking": "old", "signature": "old-sig"}, {"type": "text", "text": "old answer"}]}
    active = {"role": "assistant", "content": message()["content"]}
    tail = ({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "7"}]}
            if active_tool_turn else {"role": "user", "content": [{"type": "text", "text": "next"}]})
    messages = [USER, old, USER, active, tail]
    if not active_tool_turn:
        active = {**active, "content": active["content"][:4]}
        messages[3] = active
    body = {"model": "claude-opus-5-5", "messages": copy.deepcopy(messages), "stream": True}
    payload, _ = cc_mimicry.transform_request(cc_mimicry.ensure_request_context(body), session_id="s")
    def blocks(msg):
        return [{k: v for k, v in b.items() if k != "cache_control"} for b in msg["content"]]
    assistants = [m for m in payload["messages"] if m["role"] == "assistant"]
    assert blocks(assistants[-2]) == old["content"]
    assert blocks(assistants[-1]) == active["content"]


def test_routing_preserves_tagged_carriers_but_openai_boundary_removes_them():
    from src.failover import _attempt_body_for_channel
    from src.providers.base import OpenAIApiAdapter, OpenAICodexAdapter
    body = {"model": MODEL, "input": [USER, *output(), RESULT,
        {"type": "reasoning", "encrypted_content": "foreign-ciphertext", "summary": []}]}
    original = copy.deepcopy(body)
    portable, count = retry_body_without_encrypted_content(body, preserve_anthropic=True)
    assert count == 1
    assert portable["input"][1:4] == body["input"][1:4]
    assert _attempt_body_for_channel(body, "new-channel", "old-channel") == portable
    for adapter in (OpenAIApiAdapter(), OpenAICodexAdapter()):
        sent = adapter.filter_request_payload(body, protocol="openai-responses")
        assert len(sent["input"]) == 5
        assert sent["input"][-1]["encrypted_content"] == "foreign-ciphertext"
        assert "claude#" not in json.dumps(sent) and "opaque-redacted" not in json.dumps(sent)
    assert body == original


@pytest.mark.parametrize("model", [MODEL, "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"])
def test_retry_resolves_stored_history_without_losing_adaptive_effort_or_format(model):
    store.init()
    out = bridge.translate_response(message(), **OWNER, current_input_items=[USER])
    body = {"model": model, "reasoning": {"effort": "high"}, "previous_response_id": out["id"], "input": [RESULT],
            "text": {"format": {"type": "json_schema", "name": "answer", "schema": {"type": "object", "properties": {}}}}}
    original = copy.deepcopy(body)
    sent = bridge.translate_request(replay.retry_request(body), api_key_name=OWNER["api_key_name"], target_model=model)
    assert_fallback(sent)
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["output_config"]["effort"] == "high"
    assert body == original


@pytest.mark.parametrize("status,text,expected", [
    (400, "Invalid signature in thinking block", True),
    (400, "thinking blocks cannot be modified", True),
    (400, "Expected `thinking` or `redacted_thinking`, but found `tool_use`", True),
    (400, "thinking prefix does not match", True),
    (400, "max_tokens must exceed thinking budget_tokens", False),
    (400, "tool_result must reference a tool_use", False),
    (401, "Invalid signature in thinking block", False),
    (500, "Invalid signature in thinking block", False),
])
def test_retry_only_matches_explicit_thinking_history_rejections(status, text, expected):
    assert replay.is_thinking_rejection(status, text) is expected


@pytest.mark.parametrize("override", [
    {}, {"http_status": 200}, {"http_status": 401}, {"http_status": 403},
    {"http_status": 429}, {"http_status": 500}, {"http_status": 503},
    {"protocol": "openai-chat"}, {"protocol": "openai-responses"}, {"protocol": None},
    {"success": True}, {"stream_started": True},
    {"outcome": "guard_error"}, {"outcome": "candidate_guard"},
    {"error_detail": "max_tokens must exceed thinking budget_tokens"},
    {"error_detail": "tool_result must reference a tool_use"},
])
@pytest.mark.parametrize("json_body", [False, True])
def test_thinking_history_classification_has_the_same_bounded_scope_as_recovery(override, json_body):
    from src.protocols.runtime import AttemptResult, request_invalid_result_if_needed, should_cooldown, should_record_failure
    detail = "Invalid signature in thinking block"
    if json_body:
        detail = json.dumps({"error": {"type": "api_error", "message": detail}})
    fields = {"outcome": "http_error", "http_status": 400, "error_detail": detail}
    fields.update({k: v for k, v in override.items() if k != "protocol"})
    result = AttemptResult(**fields)
    channel = SimpleNamespace(protocol=override.get("protocol", "anthropic"))
    normalized = request_invalid_result_if_needed(result, channel=channel)
    if not override:
        assert normalized.outcome == "request_invalid" and normalized.http_status == 400
        assert not should_cooldown(normalized.outcome) and not should_record_failure(normalized.outcome)
    else:
        assert normalized.outcome == fields["outcome"]
        assert normalized.http_status == fields["http_status"]
    assert normalized.error_detail == fields["error_detail"]


async def test_native_exit_removes_only_references_to_deleted_reasoning():
    from src.openai.channel.api_channel import OpenAIApiChannel
    from src.providers.base import OpenAIApiAdapter, OpenAICodexAdapter
    native = output()[:3]
    refs = [{"type": "item_reference", "id": item["id"]} for item in native]
    kept = [USER, *output()[3:], RESULT,
            {"type": "reasoning", "id": "rs_foreign", "encrypted_content": "foreign", "summary": []},
            {"type": "item_reference", "id": "rs_foreign"},
            {"type": "item_reference", "id": "unknown"},
            {"type": "item_reference", "id": "call_1"}]
    body = {"model": MODEL, "input": [*native, *refs, *kept]}
    original = copy.deepcopy(body)
    for adapter in (OpenAIApiAdapter(), OpenAICodexAdapter()):
        assert adapter.filter_request_payload(body, protocol="openai-responses")["input"] == kept
    channel = OpenAIApiChannel({"name": "native-exit", "baseUrl": "https://fixture", "apiKey": "fixture",
                               "protocol": "openai-responses", "models": [{"alias": MODEL, "real": MODEL}]})
    up = await channel.build_upstream_request(body, MODEL, ingress_protocol="responses")
    assert json.loads(up.body)["input"] == kept
    assert body == original
    # Do not guess which duplicate-ID target a reference meant if one survives.
    retained = {"type": "message", "id": native[0]["id"], "role": "assistant", "content": "real text"}
    assert replay.without_native_items({"input": [native[0], retained, refs[0]]})["input"] == [retained, refs[0]]


def _replay_channel(model, kind, monkeypatch):
    if kind == "oauth":
        from src import oauth_manager
        from src.channel.oauth_channel import OAuthChannel
        monkeypatch.setattr(oauth_manager, "ensure_channel_token", AsyncMock(return_value="fixture"))
        return OAuthChannel({"provider": "anthropic", "email": "replay@fixture", "models": [model]})
    return ApiChannel({"name": "replay", "baseUrl": "https://api.anthropic.com", "apiKey": "fixture",
                       "protocol": "anthropic", "cc_mimicry": True, "models": [{"alias": model, "real": model}]})


@pytest.mark.parametrize("model,adaptive", [
    ("claude-sonnet-4-5", False), ("claude-opus-4-5-20251101", False),
    (MODEL, True), ("claude-sonnet-5-5", True), ("claude-opus-5-5", True), ("claude-fable-5-1", True),
])
@pytest.mark.parametrize("kind", ["api", "oauth"])
@pytest.mark.parametrize("history_kind", ["complete", "partial", "missing", "retry"])
async def test_default_thinking_explicit_and_stored_histories_are_equivalent(model, adaptive, kind, history_kind, monkeypatch):
    items = output()  # Synthetic signed blocks, not a claim of live model coverage.
    if history_kind == "partial":
        items[1].pop("encrypted_content")  # First thinking survives decoding; a later native block is missing.
    elif history_kind == "missing":
        items = items[3:]
    store.init()
    response_id = "resp_" + uuid.uuid4().hex
    store.save(response_id, None, api_key_name=OWNER["api_key_name"], model=model,
               channel_key=OWNER["channel_key"], input_items=[USER], output_items=items)
    channel = _replay_channel(model, kind, monkeypatch)
    expanded = []
    original_expand = store.expand_history

    def expand(*args, **kwargs):
        expanded.append(args[0])
        return original_expand(*args, **kwargs)

    monkeypatch.setattr(store, "expand_history", expand)
    sent = []
    for stored in (False, True):
        body = {"model": model, "_api_key_name": OWNER["api_key_name"], "input": [RESULT] if stored else [USER, *items, RESULT]}
        if stored:
            body["previous_response_id"] = response_id
        if history_kind == "retry":
            body = replay.retry_request(body)
        original = copy.deepcopy(body)
        up = await channel.build_upstream_request(body, model, ingress_protocol="responses")
        sent.append(json.loads(up.body))
        assert body == original
    assert expanded == [response_id]  # Final default repair must not query Store again.
    assert sent[0]["messages"] == sent[1]["messages"]
    assert sent[0].get("thinking") == sent[1].get("thinking")
    assert sent[0].get("output_config") == sent[1].get("output_config")
    for payload in sent:
        if adaptive:
            assert payload["thinking"]["type"] == "adaptive"
            assert payload["output_config"]["effort"] == "high"
        elif history_kind == "complete":
            assert payload["thinking"]["type"] == "enabled"
        else:
            assert "thinking" not in payload  # Omit legacy manual, never force disabled.
            assert "context_management" not in payload
        assistant = next(m for m in payload["messages"] if m["role"] == "assistant")
        if history_kind == "complete":
            assert assistant["content"][:3] == message()["content"][:3]
        else:
            assert assistant["content"] == message()["content"][3:]
        assert payload["messages"][-1]["content"][0]["content"] == "7"
        assert replay.DROP_HISTORY_KEY not in payload


def test_cc_retry_omits_only_assistants_emptied_by_thinking_removal():
    only = {"role": "assistant", "content": message()["content"][:3]}
    real = {"role": "assistant", "content": [{"type": "text", "text": "[Thinking removed]"}]}
    empty = {"role": "assistant", "content": []}
    empty_text = {"role": "assistant", "content": [{"type": "text", "text": ""}]}
    active = {"role": "assistant", "content": message()["content"]}
    result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "7"}]}
    payload = {"messages": [USER, only, USER, real, empty, empty_text, USER, active, result]}
    original = copy.deepcopy(payload)
    replay.drop_thinking_history(payload)
    assert payload["messages"] == [USER, USER, real, empty, empty_text, USER,
                                   {**active, "content": active["content"][3:]}, result]
    assert original["messages"][7] == active


def _tool_turn(sig, call_id):
    return [{"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": sig},
                                              {"type": "tool_use", "id": call_id, "name": "lookup", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": "7"}]}]


@pytest.mark.parametrize("thinking", [{"type": "adaptive"}, {"type": "enabled", "budget_tokens": 1024}])
def test_known_rejected_drop_keeps_fresh_thinking_and_thinking_mode(thinking):
    rejected = {"messages": [USER, *_tool_turn("sig-bad", "call_1")]}
    assert replay.remember_rejected_history("ch", replay.thinking_block_keys(rejected)) == 1
    payload = {"thinking": dict(thinking), "context_management": {"edits": [{"type": "clear_thinking_20251015"}]},
               "messages": [USER, *_tool_turn("sig-bad", "call_1"), *_tool_turn("sig-fresh", "call_2")]}
    assert replay.drop_known_rejected_thinking(payload, "ch") == 1
    assert [b["type"] for b in payload["messages"][1]["content"]] == ["tool_use"]
    assert payload["messages"][3]["content"][0]["signature"] == "sig-fresh"
    # The active tool turn still starts with thinking, so manual thinking stays valid.
    assert payload["thinking"] == thinking
    assert "context_management" in payload


def test_known_rejected_drop_on_active_tool_turn_omits_manual_thinking():
    replay.remember_rejected_history("ch", replay.thinking_block_keys({"messages": [USER, *_tool_turn("sig-bad", "call_1")]}))
    legacy = {"thinking": {"type": "enabled", "budget_tokens": 1024}, "messages": [USER, *_tool_turn("sig-bad", "call_1")]}
    assert replay.drop_known_rejected_thinking(legacy, "ch") == 1
    assert "thinking" not in legacy
    adaptive = {"thinking": {"type": "adaptive"}, "messages": [USER, *_tool_turn("sig-bad", "call_1")]}
    assert replay.drop_known_rejected_thinking(adaptive, "ch") == 1
    assert adaptive["thinking"] == {"type": "adaptive"}


def test_known_rejected_memory_is_channel_scoped_and_expires(monkeypatch):
    keys = replay.thinking_block_keys({"messages": [USER, *_tool_turn("sig-bad", "call_1")]})
    replay.remember_rejected_history("ch-a", keys)
    untouched = {"messages": [USER, *_tool_turn("sig-bad", "call_1")]}
    assert replay.drop_known_rejected_thinking(untouched, "ch-b") == 0
    assert replay.drop_known_rejected_thinking(copy.deepcopy(untouched), "") == 0
    assert untouched["messages"][1]["content"][0]["signature"] == "sig-bad"
    now = replay.time.monotonic()
    monkeypatch.setattr(replay.time, "monotonic", lambda: now + replay._REJECTED_TTL_S + 1)
    assert replay.drop_known_rejected_thinking(copy.deepcopy(untouched), "ch-a") == 0


@pytest.mark.parametrize("message,remember", [
    ("Invalid signature in thinking block", True),
    ("thinking blocks cannot be modified", True),
    ("thinking prefix does not match", True),
    ("Expected `thinking` or `redacted_thinking`, but found `tool_use`", False),
    ("messages.1: assistant message must start with a thinking block", False),
])
def test_only_block_attributed_rejections_are_remembered(message, remember):
    assert replay.is_thinking_rejection(400, message)
    assert replay.is_block_rejection(400, message) is remember
    assert not replay.is_block_rejection(500, message)


@pytest.mark.parametrize("kind", ["api", "oauth"])
async def test_cc_retry_does_not_invent_or_remove_placeholder_text(kind, monkeypatch):
    channel = _replay_channel(MODEL, kind, monkeypatch)
    items = output()
    real = {"role": "assistant", "content": "[Thinking removed]"}
    body = {"model": MODEL, "reasoning": {"effort": "high"}, "input": [
        USER, *items[:3], {"role": "user", "content": "next"}, real,
        {"role": "user", "content": "lookup again"}, *items, RESULT]}
    sent = []
    for source in (body, replay.retry_request(body)):
        payload = json.loads((await channel.build_upstream_request(source, MODEL, ingress_protocol="responses")).body)
        # Cache breakpoints may differ when empty turns disappear before vs after CC;
        # compare the actual visible/tool blocks, not those transport annotations.
        visible = [{k: v for k, v in block.items() if k != "cache_control"}
                   for turn in payload["messages"] for block in turn["content"]
                   if block["type"] not in ("thinking", "redacted_thinking")]
        assert sum(block.get("text") == "[Thinking removed]" for block in visible) == 1
        assert [b["type"] for b in visible] == ["text", "text", "text", "text", "text", "tool_use", "tool_result"]
        sent.append(visible)
    assert sent[0] == sent[1]
