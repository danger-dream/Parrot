"""G-01..G-07: actual Gemini codec/provider regressions, not upstream captures.

Public contracts: generateContent chunks append text; FunctionCall.id is unique,
args is a Struct; usage total includes thoughts. Cloud Code examples below only
lock down the existing private compatibility path; they do not prove its wire
contract. No test contacts Google or reads a production account/database.
"""
from __future__ import annotations

import copy
import json
import time
import uuid
from types import SimpleNamespace

import httpx

import pytest

from src.providers.antigravity_codec import (
    GeminiStreamToResponses,
    gemini_to_responses,
    responses_to_gemini,
    restore_antigravity_bytes,
    prepare_responses_request,
)


def candidate(parts, reason=None, **fields):
    result = {"content": {"parts": parts}, **fields}
    if reason is not None:
        result["finishReason"] = reason
    return {"candidates": [result]}


def frame(obj, *, wrapped=False, newline="\n"):
    if wrapped:
        obj = {"response": obj}
    return ("data: " + json.dumps(obj, ensure_ascii=False) + newline * 2).encode()


def events(raw):
    return [json.loads(line[6:]) for line in raw.decode().splitlines() if line.startswith("data: ")]


def terminal(es):
    ends = [e for e in es if e["type"] in {"response.completed", "response.incomplete", "response.failed"}]
    assert len(ends) == 1
    assert es[-1] == ends[0]
    return ends[0]["response"]


def run(objs, *, wrapped=False, mode="auto", suffix=b""):
    converter = GeminiStreamToResponses(model="gemini-fixture", stream_mode=mode)
    raw = b"".join(converter.feed(frame(obj, wrapped=wrapped)) for obj in objs)
    raw += converter.feed(suffix) + converter.close()
    assert converter.close() == b""
    return events(raw)


def calls(response):
    return [it for it in response["output"] if it["type"] == "function_call"]


def fc(name="Read", args=None, id=None):
    call = {"name": name, "args": {} if args is None else args}
    if id is not None:
        call["id"] = id
    return {"functionCall": call}


@pytest.mark.parametrize("text", ["Read file", "  中文\n", "ha"])
def test_responses_string_input_matches_message_input_without_mutation(text):
    request = {"model": "gemini-fixture", "input": text, "instructions": "system"}
    original = copy.deepcopy(request)
    got = responses_to_gemini(request)
    equivalent = responses_to_gemini({**request, "input": [{"role": "user", "content": text}]})
    assert got == equivalent
    assert got["contents"] == [{"role": "user", "parts": [{"text": text}]}]
    assert request == original


@pytest.mark.parametrize("pieces,expected", [(["ha", "ha"], "haha"), (["a", "ab"], "aab"), (["A", "B"], "AB")])
@pytest.mark.parametrize("thought", [False, True])
def test_public_text_is_incremental_even_if_equal_or_prefix(pieces, expected, thought):
    es = run([candidate([{"text": text, "thought": thought}], "STOP" if i == 1 else None)
              for i, text in enumerate(pieces)])
    kind = "response.reasoning_summary_text.delta" if thought else "response.output_text.delta"
    assert "".join(e["delta"] for e in es if e["type"] == kind) == expected
    item = terminal(es)["output"][0]
    assert (item["summary"][0]["text"] if thought else item["content"][0]["text"]) == expected


@pytest.mark.parametrize("mode,wrapped", [("auto", True), ("cloud_code_legacy", False), ("cumulative", False)])
def test_explicit_cumulative_and_existing_cloud_code_compatibility(mode, wrapped):
    es = run([candidate([{"text": "Hel"}]), candidate([{"text": "Hello"}], "STOP")], mode=mode, wrapped=wrapped)
    assert [e["delta"] for e in es if e["type"] == "response.output_text.delta"] == ["Hel", "lo"]
    assert terminal(es)["output_text"] == "Hello"


def test_verified_delta_mode_can_override_cloud_code_compatibility():
    es = run([candidate([{"text": "ha"}]), candidate([{"text": "ha"}], "STOP")], wrapped=True, mode="delta")
    assert terminal(es)["output_text"] == "haha"
    # Preserve the old mixed/snapshot branch until private captures establish
    # a stronger contract. This is a compatibility test, not Google evidence.
    legacy = run([candidate([{"text": "A"}]), candidate([{"text": "B"}], "STOP")], wrapped=True)
    assert terminal(legacy)["output_text"] == "AB"


def test_cumulative_revision_is_not_silently_appended():
    es = run([candidate([{"text": "first"}]), candidate([{"text": "revised"}], "STOP")], mode="cumulative")
    result = terminal(es)
    assert result["status"] == "failed"
    assert result["output_text"] == "first"
    assert result["provider_metadata"]["gemini"]["error"]["chunk"]["candidates"][0]["content"]["parts"][0]["text"] == "revised"


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("ids", [("one", "two"), (None, None)])
def test_distinct_tools_at_packet_index_zero_are_not_merged(wrapped, ids):
    es = run([candidate([fc("Read", {"path": "a"}, ids[0])]),
              candidate([fc("Write", {"path": "b"}, ids[1])], "STOP")], wrapped=wrapped)
    result = terminal(es)
    tools = calls(result)
    assert result["status"] == "completed"
    assert [(it["name"], json.loads(it["arguments"])) for it in tools] == [
        ("Read", {"path": "a"}), ("Write", {"path": "b"})]
    assert len({it["call_id"] for it in tools}) == 2
    assert [e["output_index"] for e in es if e["type"] == "response.output_item.added"] == [0, 1]


@pytest.mark.parametrize("wrapped", [False, True])
def test_parallel_calls_to_same_tool_keep_identity_and_reordered_updates(wrapped):
    es = run([candidate([fc(id="one", args={"x": 1}), fc(id="two", args={"x": 2})]),
              candidate([fc(id="two", args={"x": 2, "y": 3}), fc(id="one", args={"x": 1})], "STOP")], wrapped=wrapped)
    tools = calls(terminal(es))
    assert [(it["call_id"], json.loads(it["arguments"])) for it in tools] == [("one", {"x": 1}), ("two", {"x": 2, "y": 3})]
    for item in tools:
        assert "".join(e["delta"] for e in es if e["type"] == "response.function_call_arguments.delta" and e["item_id"] == item["id"]) == item["arguments"]


def test_public_calls_without_ids_are_separate_even_for_same_name():
    es = run([candidate([fc(args={"x": 1})]), candidate([fc(args={"x": 2})], "STOP")])
    assert len(calls(terminal(es))) == 2


@pytest.mark.parametrize("mode,pieces", [("delta", ['{"x":"', 'ha', 'ha', '"}']),
                                         ("cumulative", ['{"x":"', '{"x":"ha', '{"x":"haha"}'])])
def test_same_tool_string_fragments_have_explicit_mode_and_late_signature(mode, pieces):
    objs = [candidate([fc(id="one", args=p)]) for p in pieces]
    objs[-1]["candidates"][0]["content"]["parts"][0]["thoughtSignature"] = "opaque-signature"
    objs[-1]["candidates"][0]["finishReason"] = "STOP"
    tool = calls(terminal(run(objs, mode=mode)))[0]
    assert json.loads(tool["arguments"]) == {"x": "haha"}
    assert tool["encrypted_content"] == "opaque-signature"


def test_conflicting_tool_id_is_error_with_offending_payload_not_concatenation():
    result = terminal(run([candidate([fc("Read", {"a": 1}, "same")]),
                           candidate([fc("Write", {"b": 2}, "same")], "STOP")]))
    assert result["status"] == "failed"
    assert calls(result)[0]["arguments"] == '{"a":1}'
    assert result["provider_metadata"]["gemini"]["error"]["chunk"]["candidates"][0]["content"]["parts"][0]["functionCall"]["name"] == "Write"


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("wire_format", ["sse", "json"])
def test_utf8_survives_every_network_split_and_single_byte_chunks(newline, wire_format):
    obj = candidate([{"text": "中文🙂"}, fc(args={"path": "文🙂"}, id="一")], "STOP")
    wire = frame(obj, wrapped=True, newline=newline) if wire_format == "sse" else json.dumps({"response": obj}, ensure_ascii=False).encode()
    chunks_cases = [[wire[:cut], wire[cut:]] for cut in range(len(wire) + 1)] + [[bytes([b]) for b in wire]]
    for chunks in chunks_cases:
        converter = GeminiStreamToResponses(model="gemini-fixture")
        out = b"".join(converter.feed(part) for part in chunks) + converter.close()
        result = terminal(events(out)) if wire_format == "sse" else json.loads(out)
        assert result["status"] == "completed"
        assert result["output_text"] == "中文🙂"
        assert json.loads(calls(result)[0]["arguments"]) == {"path": "文🙂"}
        assert converter.close() == b""


@pytest.mark.parametrize("suffix", [b"", b"data: [DONE]\n\n"])
def test_eof_retains_partial_text_and_complete_tools_without_claiming_success(suffix):
    es = run([candidate([fc(id="one", args={"path": "a"}), {"text": "partial"}])], suffix=suffix)
    result = terminal(es)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "upstream_stream_incomplete"
    assert result["output_text"] == "partial"
    assert [it["type"] for it in result["output"]] == ["function_call", "message"]
    assert calls(result)[0]["status"] == "completed"  # recoverable item, not a fabricated full response
    assert result["output"][1]["status"] == "incomplete"


def test_eof_retains_invalid_tool_fragment_without_executable_done_or_raw_wrapper():
    es = run([candidate([fc(id="one", args='{"path":')])])
    result = terminal(es)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "upstream_stream_incomplete"
    assert calls(result)[0]["arguments"] == '{"path":'
    assert calls(result)[0]["status"] == "incomplete"
    assert not any(e["type"] == "response.function_call_arguments.done" for e in es)


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("reason,status,detail", [
    ("STOP", "completed", None), ("MAX_TOKENS", "incomplete", "max_output_tokens"),
    ("SAFETY", "incomplete", "content_filter"), ("SPII", "incomplete", "content_filter"),
    ("IMAGE_SAFETY", "incomplete", "content_filter"),
    ("MALFORMED_FUNCTION_CALL", "failed", None), ("UNEXPECTED_TOOL_CALL", "failed", None),
    ("OTHER", "failed", None), ("NEW_REASON", "failed", None),
    (None, "failed", None),
])
def test_finish_reason_json_and_stream_agree_and_preserve_upstream_reason(wrapped, reason, status, detail):
    obj = candidate([{"text": "kept"}], reason, finishMessage="provider explanation")
    results = [gemini_to_responses({"response": obj} if wrapped else obj, model="fixture"), terminal(run([obj], wrapped=wrapped))]
    for result in results:
        assert result["status"] == status
        assert (result["incomplete_details"] or {}).get("reason") == detail
        assert result["output_text"] == "kept"
        assert result["provider_metadata"]["gemini"].get("finishReason") == reason
        assert result["provider_metadata"]["gemini"]["finishMessage"] == "provider explanation"
        assert bool(result["error"]) == (status == "failed")


@pytest.mark.parametrize("wrapped", [False, True])
def test_error_and_prompt_feedback_keep_original_information_and_usage(wrapped):
    feedback = {"blockReason": "SAFETY", "safetyRatings": [{"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "probability": "HIGH"}]}
    error = {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "fixture error", "details": [{"retryDelay": "1s"}]}
    for field, value, status in [("promptFeedback", feedback, "incomplete"), ("error", error, "failed")]:
        obj = {**candidate([{"text": "keep partial"}]), field: value, "usageMetadata": {"promptTokenCount": 10}}
        for result in [gemini_to_responses({"response": obj} if wrapped else obj, model="fixture"), terminal(run([obj], wrapped=wrapped))]:
            assert result["status"] == status
            assert result["provider_metadata"]["gemini"][field] == value
            assert result["output_text"] == "keep partial"
            assert result["usage"]["input_tokens"] == 10
    # A block-only envelope must be unwrapped even without candidates/usage.
    obj = {"promptFeedback": feedback}
    assert terminal(run([obj], wrapped=wrapped))["status"] == "incomplete"
    assert gemini_to_responses({"response": obj} if wrapped else obj, model="fixture")["status"] == "incomplete"


@pytest.mark.parametrize("total", [None, 35, 0, 999])
def test_usage_includes_thoughts_and_preserves_authoritative_total(total):
    meta = {"promptTokenCount": 10, "candidatesTokenCount": 5, "thoughtsTokenCount": 20, "cachedContentTokenCount": 3}
    if total is not None:
        meta["totalTokenCount"] = total
    obj = {**candidate([{"text": "ok"}], "STOP"), "usageMetadata": meta}
    for result in [gemini_to_responses(obj, model="fixture"), terminal(run([obj]))]:
        assert result["usage"] == {"input_tokens": 10, "output_tokens": 25, "total_tokens": 35 if total is None else total,
                                   "input_tokens_details": {"cached_tokens": 3}, "output_tokens_details": {"reasoning_tokens": 20}}


def test_partial_usage_updates_do_not_erase_prior_counts():
    result = terminal(run([{"usageMetadata": {"promptTokenCount": 10, "thoughtsTokenCount": 20}},
                           {**candidate([], "STOP"), "usageMetadata": {"candidatesTokenCount": 5, "totalTokenCount": 35}}]))
    assert result["usage"]["output_tokens"] == 25
    assert result["usage"]["input_tokens"] == 10


def test_invalid_utf8_and_sse_json_are_errors_not_replacement_text_or_empty_success():
    for wire, code in [(b'data: {"bad":\xff}\n\n', "invalid_upstream_utf8"),
                       (b'data: {bad}\n\n', "invalid_upstream_json"),
                       (b'data: {"text":"\xe4', "invalid_upstream_utf8")]:
        converter = GeminiStreamToResponses(model="fixture")
        result = terminal(events(converter.feed(wire) + converter.close()))
        assert result["status"] == "failed"
        assert result["error"]["code"] == code


def test_terminal_is_unique_and_final_even_with_extra_packets_and_close():
    converter = GeminiStreamToResponses(model="fixture")
    wire = frame(candidate([{"text": "ok"}], "STOP")) + frame(candidate([{"text": "late"}]))
    es = events(converter.feed(wire) + converter.close() + converter.feed(wire))
    assert terminal(es)["output_text"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_real_channel_request_and_provider_restore_use_fixed_codec(monkeypatch, stream):
    from src import oauth_manager
    from src.channel.antigravity_oauth_channel import AntigravityOAuthChannel
    from src.providers import registry

    async def token(_):
        return "local-test-token"
    monkeypatch.setattr(oauth_manager, "ensure_channel_token", token)
    channel = AntigravityOAuthChannel({"provider": "antigravity", "email": "fixture@example.test", "project_id": "fixture-project", "models": ["gemini-fixture"]})
    req = await channel.build_upstream_request({"model": "gemini-fixture", "input": "Read file", "stream": stream}, "gemini-fixture", ingress_protocol="responses")
    assert json.loads(req.body)["request"]["contents"] == [{"role": "user", "parts": [{"text": "Read file"}]}]
    assert ("streamGenerateContent" in req.url) == stream
    objs = [candidate([fc("Read", {"path": "a"}, "one")]), candidate([fc("Write", {"path": "b"}, "two")], "STOP")]
    if stream:
        wire = b"".join(frame(obj, wrapped=True) for obj in objs)
        raw = b"".join([await registry.restore_response_bytes(channel, bytes([b]), translator_ctx=req.translator_ctx) for b in wire])
        result = terminal(events(raw))
    else:
        obj = candidate([fc("Read", {"path": "a"}, "one"), fc("Write", {"path": "b"}, "two")], "STOP")
        raw = await registry.restore_response_bytes(channel, json.dumps({"response": obj}).encode(), translator_ctx=req.translator_ctx)
        result = json.loads(raw)
    assert [(c["call_id"], c["name"]) for c in calls(result)] == [("one", "Read"), ("two", "Write")]
    assert result["status"] == "completed"


@pytest.mark.parametrize("target", ["chat", "anthropic"])
def test_eof_stays_error_through_actual_downstream_bridge(target):
    from src.openai.transform import stream_r2c, stream_responses_to_anthropic
    codec = GeminiStreamToResponses(model="fixture")
    raw = codec.feed(frame(candidate([{"text": "partial"}]))) + codec.close()
    module = stream_r2c if target == "chat" else stream_responses_to_anthropic
    bridge = module.StreamTranslator(model="fixture")
    out = b"".join(bridge.feed(raw)) + b"".join(bridge.close())
    es = [json.loads(line[6:]) for line in out.decode().splitlines()
          if line.startswith("data: ") and line != "data: [DONE]"]
    assert any(e.get("error") for e in es)
    assert not any(c.get("finish_reason") == "stop" for e in es for c in e.get("choices", []))
    assert not any(e.get("type") == "message_stop" for e in es)


def test_unspecified_finish_reason_does_not_end_stream_prematurely():
    result = terminal(run([candidate([{"text": "a"}], "FINISH_REASON_UNSPECIFIED"),
                           candidate([{"text": "b"}], "STOP")]))
    assert result["output_text"] == "ab"
    assert result["status"] == "completed"


@pytest.fixture
def local_channel(monkeypatch):
    from src import oauth_manager
    from src.channel.antigravity_oauth_channel import AntigravityOAuthChannel

    async def token(_):
        return "local-test-token"
    monkeypatch.setattr(oauth_manager, "ensure_channel_token", token)
    return AntigravityOAuthChannel({"provider": "antigravity", "email": "fixture@example.test",
                                   "project_id": "fixture-project", "models": ["gemini-fixture"]})


def namespace_tools():
    return [{"type": "function", "name": "fs__Read", "parameters": {"type": "object"}},
            {"type": "namespace", "name": "fs", "description": "Files only", "tools": [
                {"type": "function", "name": "Read", "description": "Read a path", "parameters": {
                    "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}]}]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_namespace_local_store_and_previous_response_round_trip(local_channel, stream):
    from src.openai import store
    from src.openai.transform.guard import GuardError
    from src.providers import registry
    store.init()  # isolated runner owns all paths, including this SQLite file.
    key = "gemini-key-" + uuid.uuid4().hex
    body = {"model": "gemini-fixture", "_api_key_name": key, "stream": stream,
            "input": "Read file", "tools": namespace_tools(),
            "tool_choice": {"type": "function", "namespace": "fs", "name": "Read"}}
    original = copy.deepcopy(body)
    req = await local_channel.build_upstream_request(body, "gemini-fixture", ingress_protocol="responses")
    wire = json.loads(req.body)["request"]
    declarations = wire["tools"][0]["functionDeclarations"]
    assert len(declarations) == 2
    flat = declarations[1]["name"]
    assert flat != "fs__Read"  # direct/name collision was resolved reversibly
    assert "Files only" in declarations[1]["description"]
    assert wire["toolConfig"]["functionCallingConfig"]["allowedFunctionNames"] == [flat]
    assert body == original
    obj = candidate([fc(flat, {"path": "fs__Read"}, "tool-one")], "STOP")
    raw = frame(obj, wrapped=True) if stream else json.dumps({"response": obj}).encode()
    restored = await registry.restore_response_bytes(local_channel, raw, translator_ctx=req.translator_ctx)
    result = terminal(events(restored)) if stream else json.loads(restored)
    tool = calls(result)[0]
    assert (tool["name"], tool["namespace"], tool["call_id"]) == ("Read", "fs", "tool-one")
    assert json.loads(tool["arguments"])["path"] == "fs__Read"  # never rewrite argument text
    if stream:
        for e in events(restored):
            if e["type"] in {"response.output_item.added", "response.output_item.done"}:
                assert e["item"]["name"] == "Read"
                assert e["item"]["namespace"] == "fs"
    assert result["store"] is True
    saved = store.lookup(result["id"], api_key_name=key)
    assert saved.output_items[0] == tool
    assert saved.input_items[0]["content"][0]["text"] == "Read file"
    followup = {"model": "gemini-fixture", "_api_key_name": key, "previous_response_id": result["id"],
                "input": [{"type": "function_call_output", "call_id": "tool-one", "output": "file bytes"}],
                "tools": namespace_tools()}
    from src.protocols.matrix import DEFAULT_MATRIX, capabilities_for_channel, extract_request_features
    DEFAULT_MATRIX.plan("responses", "openai-responses", extract_request_features("responses", followup),
                        capabilities_for_channel(local_channel))
    next_req = await local_channel.build_upstream_request(followup, "gemini-fixture", ingress_protocol="responses")
    contents = json.loads(next_req.body)["request"]["contents"]
    assert contents[0]["parts"][0]["text"] == "Read file"
    assert contents[1]["parts"][0]["functionCall"] == {"id": "tool-one", "name": flat, "args": {"path": "fs__Read"}}
    assert contents[2]["parts"][0]["functionResponse"] == {"id": "tool-one", "name": flat, "response": {"result": "file bytes"}}
    with pytest.raises(GuardError) as forbidden:
        await local_channel.build_upstream_request({**followup, "_api_key_name": key + "-other"}, "gemini-fixture", ingress_protocol="responses")
    assert forbidden.value.status == 403


@pytest.mark.parametrize("choice_mode", ["auto", "required"])
def test_local_reference_and_namespace_allowed_tools_are_real_conversions(choice_mode):
    request = {"input": [{"type": "message", "id": "msg-local", "role": "user", "content": "Read"},
                         {"type": "item_reference", "id": "msg-local"}],
               "tools": namespace_tools(),
               "tool_choice": {"type": "allowed_tools", "mode": choice_mode,
                               "tools": [{"type": "function", "namespace": "fs", "name": "Read"}]}}
    original = copy.deepcopy(request)
    prepared, plan = prepare_responses_request(request)
    wire = responses_to_gemini(prepared)
    assert responses_to_gemini(request) == wire
    assert [c["parts"][0]["text"] for c in wire["contents"]] == ["Read", "Read"]
    declarations = wire["tools"][0]["functionDeclarations"]
    assert len(declarations) == 1
    identity = plan.identity_for_flat(declarations[0]["name"])
    assert (identity.namespace, identity.child_name) == ("fs", "Read")
    assert wire["toolConfig"]["functionCallingConfig"]["mode"] == ("AUTO" if choice_mode == "auto" else "ANY")
    assert request == original


@pytest.mark.parametrize("patch", [
    {"input": [{"type": "item_reference", "id": "unresolved"}]},
    {"conversation": "conv-server-only"}, {"background": True},
    {"tools": [{"type": "file_search", "vector_store_ids": ["vs-server-only"]}]},
    {"tools": [{"type": "tool_search"}]},
    {"input": [{"type": "web_search_call", "id": "server-only"}]},
    {"input": [{"role": "user", "content": [{"type": "input_file", "file_id": "file-server-only"}]}]},
])
def test_irreducible_server_state_is_explicit_not_dropped(patch):
    from src.openai.transform.guard import GuardError
    with pytest.raises(GuardError):
        prepare_responses_request({"input": "ok", **patch})


@pytest.mark.asyncio
async def test_store_false_and_missing_history_do_not_advertise_native_state(local_channel):
    from src.openai import store
    from src.openai.transform.guard import GuardError
    from src.providers import registry
    store.init()
    key = "gemini-no-store-" + uuid.uuid4().hex
    req = await local_channel.build_upstream_request({"input": "hi", "store": False, "_api_key_name": key}, "gemini-fixture", ingress_protocol="responses")
    raw = await registry.restore_response_bytes(local_channel, json.dumps(candidate([{"text": "ok"}], "STOP")).encode(), translator_ctx=req.translator_ctx)
    result = json.loads(raw)
    assert result["store"] is False
    with pytest.raises(store.ResponseNotFound):
        store.lookup(result["id"], api_key_name=key)
    with pytest.raises(GuardError) as missing:
        await local_channel.build_upstream_request({"input": "next", "previous_response_id": result["id"], "_api_key_name": key}, "gemini-fixture", ingress_protocol="responses")
    assert missing.value.status == 404


@pytest.mark.asyncio
async def test_store_write_failure_preserves_generation_without_fake_resumability(local_channel, monkeypatch):
    from src.openai import store
    from src.providers import registry
    def fail(*args, **kwargs):
        raise OSError("isolated store fixture unavailable")
    monkeypatch.setattr(store, "save", fail)
    req = await local_channel.build_upstream_request({"input": "hi", "store": True}, "gemini-fixture", ingress_protocol="responses")
    raw = await registry.restore_response_bytes(local_channel, json.dumps(candidate([{"text": "kept"}], "STOP")).encode(), translator_ctx=req.translator_ctx)
    result = json.loads(raw)
    assert result["status"] == "completed"
    assert result["output_text"] == "kept"
    assert result["store"] is False
    assert result["provider_metadata"]["gemini"]["local_store"] == "unavailable"


def test_antigravity_matrix_distinguishes_native_from_translated_state(local_channel):
    from src.protocols.matrix import capabilities_for_channel
    caps = capabilities_for_channel(local_channel)
    assert caps.transports == frozenset({"http-json", "http-sse"})
    assert caps.native_state == frozenset({"thought_signature_replay", "session_id"})
    assert caps.translated_state == frozenset({"previous_response_id", "item_reference", "namespace",
                                             "encrypted_reasoning_replay", "prompt_cache_key"})
    for provider in ("openai", "xai"):
        other = capabilities_for_channel(SimpleNamespace(protocol="openai-responses", type="oauth", provider=provider))
        assert not other.translated_state
        assert ("ws" in other.transports) == (provider == "openai")
        assert ("namespace" in other.native_state) == (provider == "openai")


@pytest.mark.asyncio
async def test_matrix_allows_real_namespace_and_local_reference_conversion(local_channel):
    from src.protocols.matrix import DEFAULT_MATRIX, capabilities_for_channel, extract_request_features
    body = {"input": [{"type": "message", "id": "local-msg", "role": "user", "content": "kept"},
                      {"type": "item_reference", "id": "local-msg"}], "tools": namespace_tools()}
    caps = capabilities_for_channel(local_channel)
    plan = DEFAULT_MATRIX.plan("responses", "openai-responses", extract_request_features("responses", body), caps)
    assert plan.upstream_protocol == "openai-responses"
    req = await local_channel.build_upstream_request(body, "gemini-fixture", ingress_protocol="responses")
    wire = json.loads(req.body)["request"]
    assert [item["parts"][0]["text"] for item in wire["contents"]] == ["kept", "kept"]
    assert len(wire["tools"][0]["functionDeclarations"]) == 2


@pytest.mark.parametrize("patch", [
    {"input": [{"type": "item_reference", "id": "unknown-server-item"}]},
    {"tools": [{"type": "custom", "name": "raw"}]},
    {"tools": [{"type": "custom", "name": "grammar", "format": {"type": "grammar", "syntax": "regex", "definition": "a+"}}]},
    {"tools": namespace_tools() + [{"type": "file_search"}]},
    {"tools": [{"type": "tool_search"}]},
    {"conversation": "server-only"}, {"background": True},
])
def test_matrix_does_not_invent_gemini_server_capabilities(local_channel, patch):
    from src.protocols.matrix import DEFAULT_MATRIX, ProtocolGuardError, capabilities_for_channel, extract_request_features
    body = {"input": "ok", **patch}
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan("responses", "openai-responses", extract_request_features("responses", body),
                            capabilities_for_channel(local_channel))


class _HttpChunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class _HttpContext:
    closed = False

    async def __aexit__(self, *args):
        self.closed = True


def _observe_close(monkeypatch, converter):
    original = converter.close
    observations = []
    def close():
        out = original()
        observations.append(out)
        return out
    monkeypatch.setattr(converter, "close", close)
    return observations


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", ["partial_text", "partial_tool", "buffered_stop", "normal_stop", "precommit_buffered_stop", "buffered_error"])
async def test_http_stream_eof_flushes_actual_provider_and_codec(local_channel, monkeypatch, layout):
    from src.transports.http_runtime import prepare_stream_response_start, read_next_stream_step
    req = await local_channel.build_upstream_request({"input": "hi", "stream": True, "store": False}, "gemini-fixture", ingress_protocol="responses")
    observed = _observe_close(monkeypatch, req.translator_ctx["antigravity_stream"])
    first = frame(candidate([{"text": "中文🙂"}]), wrapped=True)
    ending = frame(candidate([], "STOP"), wrapped=True)
    if layout == "partial_text":
        chunks = [first]
    elif layout == "partial_tool":
        chunks = [frame(candidate([fc(id="one", args={"path": "a"})]), wrapped=True)]
    elif layout == "normal_stop":
        chunks = [first, ending]
    elif layout == "buffered_stop":
        chunks = [first, ending[:-2]]
    elif layout == "precommit_buffered_stop":
        chunks = [frame(candidate([{"text": "中文🙂"}], "STOP"), wrapped=True)[:-2]]
    else:
        chunks = [first, frame({"error": {"code": 429, "message": "fixture quota"}}, wrapped=True)[:-2]]
    response = httpx.Response(200, stream=_HttpChunks(chunks), headers={"content-type": "text/event-stream"})
    now = time.time()
    start = await prepare_stream_response_start(
        _HttpContext(), response, local_channel, dynamic_map=None, connect_ms=1,
        deadline_ts=now + 30, first_byte_timeout=5, idle_timeout=5, ingress_protocol="responses",
        translator_ctx=req.translator_ctx,
    )
    assert start.ok, start.error
    output = list(start.first_downstream_chunks)
    for _ in range(8):
        step = await read_next_stream_step(
            aiter=start.aiter, channel=local_channel, dynamic_map=None, tracker=start.tracker,
            builder=start.builder, stream_translator=start.stream_translator,
            deadline_ts=now + 30, start_time=now, idle_timeout=5,
            translator_ctx=req.translator_ctx, upstream_status=200,
        )
        if step.kind == "end":
            break
        assert step.kind == "chunks"
        output.extend(step.downstream_chunks)
    else:
        pytest.fail("EOF flush did not terminate")
    assert len(observed) == 1  # transport -> registry -> actual adapter -> codec.close
    result = terminal(events(b"".join(output)))
    assert result["status"] == ("completed" if "stop" in layout else "failed")
    if layout == "partial_tool":
        assert calls(result)[0]["arguments"] == '{"path":"a"}'
        assert calls(result)[0]["status"] == "completed"
    else:
        assert result["output_text"] == "中文🙂"
    if layout.startswith("partial"):
        assert result["error"]["code"] == "upstream_stream_incomplete"
    if layout == "buffered_error":
        assert result["error"]["code"] == "429"


@pytest.mark.asyncio
async def test_precommit_http_eof_flush_reports_truncated_utf8(local_channel, monkeypatch):
    from src.transports.http_runtime import prepare_stream_response_start
    req = await local_channel.build_upstream_request({"input": "hi", "stream": True, "store": False}, "gemini-fixture", ingress_protocol="responses")
    observed = _observe_close(monkeypatch, req.translator_ctx["antigravity_stream"])
    raw = frame(candidate([{"text": "中文"}], "STOP"), wrapped=True)
    response = httpx.Response(200, stream=_HttpChunks([raw[:raw.index("中".encode()) + 1]]), headers={"content-type": "text/event-stream"})
    ctx = _HttpContext()
    result = await prepare_stream_response_start(
        ctx, response, local_channel, dynamic_map=None, connect_ms=1, deadline_ts=time.time() + 30,
        first_byte_timeout=5, idle_timeout=5, ingress_protocol="responses", translator_ctx=req.translator_ctx,
    )
    assert not result.ok
    assert ctx.closed
    assert len(observed) == 1
    assert "invalid_upstream_utf8" in result.error.error_detail
    assert terminal(events(result.error.full_response_text.encode()))["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", [None, "STOP", "MAX_TOKENS"])
async def test_http_aggregate_eof_flush_keeps_terminal_output_and_usage(local_channel, monkeypatch, finish):
    from src.transports.http_runtime import aggregate_stream_as_non_stream_response
    req = await local_channel.build_upstream_request({"input": "hi", "stream": True, "store": False}, "gemini-fixture", ingress_protocol="responses")
    observed = _observe_close(monkeypatch, req.translator_ctx["antigravity_stream"])
    obj = candidate([{"text": "kept"}, fc(id="one", args={"path": "a"})], finish)
    obj["usageMetadata"] = {"promptTokenCount": 10, "candidatesTokenCount": 5, "thoughtsTokenCount": 20, "totalTokenCount": 35}
    # Deliberately no trailing SSE blank line: only provider EOF flush can
    # restore the terminal frame, not a direct test call to the converter.
    response = httpx.Response(200, stream=_HttpChunks([frame(obj, wrapped=True)[:-2]]), headers={"content-type": "text/event-stream"})
    ctx, now = _HttpContext(), time.time()
    result = await aggregate_stream_as_non_stream_response(
        ctx, response, local_channel, "gemini-fixture", dynamic_map=None, connect_ms=1,
        start_time=now, deadline_ts=now + 30, total_timeout=30, first_byte_timeout=5,
        idle_timeout=5, translator_ctx=req.translator_ctx,
    )
    assert ctx.closed
    assert len(observed) == 1
    if finish is None:
        assert result.error is not None
        out = terminal(events(result.error.full_response_text.encode()))
        assert out["status"] == "failed"
        assert out["error"]["code"] == "upstream_stream_incomplete"
    else:
        assert result.ok, result.error
        out = result.obj
        assert out["status"] == ("completed" if finish == "STOP" else "incomplete")
    assert out["output_text"] == "kept"
    assert calls(out)[0]["arguments"] == '{"path":"a"}'
    assert out["usage"]["output_tokens"] == 25
    assert out["usage"]["total_tokens"] == 35


@pytest.mark.asyncio
async def test_default_provider_eof_flush_is_noop():
    from src.providers import registry
    class OtherChannel:
        protocol = "openai-responses"
        type = "api"
        async def restore_response(self, *args, **kwargs):
            pytest.fail("default EOF must not call ordinary restore with synthetic empty bytes")
    assert await registry.finish_response_bytes(OtherChannel()) == b""


def test_restore_flush_preserves_eof_recovery_and_is_idempotent():
    converter = GeminiStreamToResponses(model="fixture")
    raw = restore_antigravity_bytes(frame(candidate([{"text": "partial"}])), converter=converter)
    raw += restore_antigravity_bytes(b"", converter=converter, flush=True)
    assert terminal(events(raw))["status"] == "failed"
    assert restore_antigravity_bytes(b"", converter=converter, flush=True) == b""


@pytest.mark.parametrize("raw_args,expected_args", [
    ('{"action":"edit_file","params":{"path":"/a"}', {"_raw": '{"action":"edit_file","params":{"path":"/a"}'}),
    ("[1, 2, 3]", {"_raw": "[1, 2, 3]"}),
    ('{"x": NaN}', {"_raw": '{"x": NaN}'}),
    ("", {}),
    (None, {}),
    ({"ok": True}, {"ok": True}),
])
def test_historical_malformed_function_call_arguments_replay_as_gemini_struct(raw_args, expected_args):
    request = {
        "model": "gemini-4-neon",
        "input": [
            {"role": "user", "content": "edit file"},
            {"type": "function_call", "call_id": "call_bad_1", "name": "Servers", "arguments": raw_args},
            {"type": "function_call_output", "call_id": "call_bad_1", "output": "error: 工具参数必须是 JSON 对象"},
            {"role": "user", "content": "continue"},
        ],
    }
    prepared, _ = prepare_responses_request(request)
    wire = responses_to_gemini(prepared)
    fc_part = wire["contents"][1]["parts"][0]["functionCall"]
    fr_part = wire["contents"][2]["parts"][0]["functionResponse"]
    assert fc_part == {"name": "Servers", "args": expected_args, "id": "call_bad_1"}
    assert fr_part == {"name": "Servers", "id": "call_bad_1", "response": {"result": "error: 工具参数必须是 JSON 对象"}}


def test_function_call_without_valid_name_still_fails_with_typed_guard_code():
    from src.openai.transform.guard import GuardError
    with pytest.raises(GuardError) as exc_info:
        prepare_responses_request({
            "input": [{"type": "function_call", "call_id": "call_noname", "name": "  ", "arguments": "{}"}],
        })
    assert exc_info.value.status == 400
    assert exc_info.value.scope == "candidate"
    assert exc_info.value.code == "antigravity_invalid_function_call"

