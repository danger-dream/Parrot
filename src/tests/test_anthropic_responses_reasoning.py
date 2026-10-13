"""Readable Anthropic thinking must survive the Responses return bridge."""
from __future__ import annotations

import json

import pytest

from src import config
from src.openai import store
from src.openai.transform import responses_to_anthropic
from src.protocols.commit_gate import SseCommitGate
from src.protocols.runtime import apply_non_stream_response_translator, make_stream_translator


CTX = {
    "response_translator": "responses_to_anthropic",
    "model_for_response": "GLM-5.3",
    "allow_unsigned_thinking": True,
    "request_body": {"model": "GLM-5.3", "reasoning": {"effort": "max"}},
}


@pytest.fixture(autouse=True)
def passthrough(monkeypatch):
    monkeypatch.setitem(config.get().setdefault("openai", {}), "reasoningBridge", "passthrough")


def _wire(*events):
    return b"".join(
        ("event: " + event["type"] + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()
        for event in events
    )


def _start(index, block):
    return {"type": "content_block_start", "index": index, "content_block": block}


def _delta(index, typ, **values):
    return {"type": "content_block_delta", "index": index, "delta": {"type": typ, **values}}


def _stop(index):
    return {"type": "content_block_stop", "index": index}


def _events(chunks):
    return [json.loads(line[6:]) for line in b"".join(chunks).decode().splitlines() if line.startswith("data: ")]


def _reasoning_stream():
    return _wire(
        {"type": "message_start", "message": {"model": "GLM-5.3", "usage": {"input_tokens": 5}}},
        _start(0, {"type": "thinking", "thinking": "先"}),
        _delta(0, "thinking_delta", thinking="分析🌙"),
        _delta(0, "signature_delta", signature="anthropic-signature"),
        _stop(0),
        _start(1, {"type": "redacted_thinking", "data": "redacted-secret"}),
        _stop(1),
        _start(2, {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {}}),
        _delta(2, "input_json_delta", partial_json='{"q":"x"}'),
        _stop(2),
        _start(3, {"type": "thinking", "thinking": ""}),
        _delta(3, "thinking_delta", thinking="再检查"),
        _stop(3),
        _start(4, {"type": "text", "text": ""}),
        _delta(4, "text_delta", text="答案"),
        _stop(4),
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 7}},
        {"type": "message_stop"},
    )


@pytest.mark.parametrize("bytewise", [False, True])
def test_stream_thinking_events_final_items_and_tool_order(bytewise, monkeypatch):
    saved = []
    monkeypatch.setattr(store, "is_enabled", lambda: True)
    monkeypatch.setattr(store, "save", lambda **kw: saved.append(kw))
    tr = make_stream_translator({**CTX, "api_key_name": "test", "current_input_items": []})
    wire = _reasoning_stream()
    pieces = (bytes([b]) for b in wire) if bytewise else [wire]
    chunks = [chunk for piece in pieces for chunk in tr.feed(piece)] + list(tr.close())
    assert list(tr.close()) == []
    events = _events(chunks)
    assert [e["sequence_number"] for e in events] == list(range(1, len(events) + 1))
    assert [e["delta"] for e in events if e["type"] == "response.reasoning_summary_text.delta"] == ["先", "分析🌙", "再检查"]
    done = [e for e in events if e["type"] == "response.reasoning_summary_text.done"]
    assert [(e["output_index"], e["text"]) for e in done] == [(0, "先分析🌙"), (2, "再检查")]
    final = events[-1]["response"]
    output = final["output"]
    assert [item["type"] for item in output] == ["reasoning", "function_call", "reasoning", "message"]
    assert output[0]["summary"] == [{"type": "summary_text", "text": "先分析🌙"}]
    assert output[2]["summary"] == [{"type": "summary_text", "text": "再检查"}]
    assert output[1]["arguments"] == '{"q":"x"}'
    assert final["output_text"] == "答案"
    assert final["usage"]["input_tokens"] == 5
    assert final["usage"]["output_tokens"] == 7
    assert tr.get_downstream_responses_output() == saved[0]["output_items"] == output
    added = [e for e in events if e["type"] == "response.output_item.added"]
    assert [e["output_index"] for e in added] == [0, 1, 2, 3]
    for event in [e for e in events if e["type"] == "response.output_item.done"]:
        assert event["item"] == output[event["output_index"]]
    raw = b"".join(chunks)
    assert b"encrypted_content" not in raw
    assert b"anthropic-signature" not in raw
    assert b"redacted-secret" not in raw


@pytest.mark.parametrize("bytewise", [False, True])
@pytest.mark.parametrize("blocks", [
    ("thinking", "text", "tool_use"),
    ("text", "tool_use", "text", "tool_use"),
    ("tool_use", "text"),
    ("text", "text"),
])
def test_each_text_block_finishes_in_place_without_merging_across_tools(blocks, bytewise, monkeypatch):
    saved = []
    monkeypatch.setattr(store, "is_enabled", lambda: True)
    monkeypatch.setattr(store, "save", lambda **kw: saved.append(kw))
    tr = make_stream_translator({**CTX, "api_key_name": "test", "current_input_items": []})
    chunks = []
    expected_text = []
    for index, typ in enumerate(blocks):
        if typ == "text":
            block = {"type": "text", "text": f"start-{index}:"}
            delta = _delta(index, "text_delta", text=f"end-{index}")
            expected_text.append(f"start-{index}:end-{index}")
        elif typ == "thinking":
            block = {"type": "thinking", "thinking": ""}
            delta = _delta(index, "thinking_delta", thinking="consider")
        else:
            block = {"type": "tool_use", "id": f"call_{index}", "name": "lookup", "input": {}}
            delta = _delta(index, "input_json_delta", partial_json='{"q":"x"}')
        wire = _wire(_start(index, block), delta, _stop(index))
        pieces = (bytes([b]) for b in wire) if bytewise else [wire]
        chunks += [chunk for piece in pieces for chunk in tr.feed(piece)]
        done = [e for e in _events(chunks) if e["type"] == "response.output_item.done"]
        # The text item must finish at its own block_stop, before the next tool starts.
        assert [e["output_index"] for e in done] == list(range(index + 1))
    chunks += list(tr.feed(_wire(
        {"type": "message_delta", "delta": {"stop_reason": "tool_use" if "tool_use" in blocks else "end_turn"}},
        {"type": "message_stop"},
    )))
    chunks += list(tr.close())
    assert list(tr.close()) == []
    events = _events(chunks)
    output = events[-1]["response"]["output"]
    expected_types = [{"thinking": "reasoning", "text": "message", "tool_use": "function_call"}[typ] for typ in blocks]
    assert [item["type"] for item in output] == expected_types
    assert len({item["id"] for item in output}) == len(output)
    assert [item["content"][0]["text"] for item in output if item["type"] == "message"] == expected_text
    assert events[-1]["response"]["output_text"] == "".join(expected_text)
    assert [e["sequence_number"] for e in events] == list(range(1, len(events) + 1))
    done = [e for e in events if e["type"] == "response.output_item.done"]
    assert [e["item"] for e in done] == output == saved[0]["output_items"]
    for index, item in enumerate(output):
        if item["type"] == "message":
            text_deltas = [e for e in events if e["type"] == "response.output_text.delta" and e["output_index"] == index]
            assert "".join(e["delta"] for e in text_deltas) == item["content"][0]["text"]
            assert all(e["item_id"] == item["id"] for e in text_deltas)


@pytest.mark.parametrize("stop_reason", ["end_turn", "max_tokens"])
def test_close_finishes_unstopped_text_once(stop_reason):
    tr = make_stream_translator(CTX)
    chunks = list(tr.feed(_wire(
        _start(0, {"type": "text", "text": "initial"}),
        _delta(0, "text_delta", text=" tail"),
        {"type": "message_delta", "delta": {"stop_reason": stop_reason}},
        {"type": "message_stop"},
    ))) + list(tr.close())
    events = _events(chunks)
    done = [e for e in events if e["type"] == "response.output_item.done"]
    assert len(done) == 1
    assert done[0]["item"]["content"][0]["text"] == "initial tail"
    assert events[-1]["response"]["output"] == [done[0]["item"]]
    assert events[-1]["type"] == ("response.incomplete" if stop_reason == "max_tokens" else "response.completed")
    assert list(tr.close()) == []


def test_thinking_is_visible_through_production_commit_gate():
    tr = make_stream_translator(CTX)
    gate = SseCommitGate(protocol="responses", stream_translator=tr)
    result = gate.feed(_wire(
        {"type": "message_start", "message": {"model": "GLM-5.3"}},
        _start(0, {"type": "thinking", "thinking": ""}),
        _delta(0, "thinking_delta", thinking="分析"),
    ))
    assert result.error_event is None
    assert any(e["type"] == "response.reasoning_summary_text.delta" for e in _events(result.downstream_chunks))


def test_close_finishes_reasoning_without_block_stop_once():
    tr = make_stream_translator(CTX)
    chunks = list(tr.feed(_wire(
        _start(0, {"type": "thinking", "thinking": "已有思考"}),
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}},
        {"type": "message_stop"},
    ))) + list(tr.close())
    events = _events(chunks)
    assert sum(e["type"] == "response.reasoning_summary_text.done" for e in events) == 1
    assert events[-1]["type"] == "response.incomplete"
    assert events[-1]["response"]["output"][0]["summary"][0]["text"] == "已有思考"
    assert list(tr.close()) == []


def test_stream_respects_explicit_drop(monkeypatch):
    monkeypatch.setitem(config.get()["openai"], "reasoningBridge", "drop")
    tr = make_stream_translator(CTX)
    events = _events(list(tr.feed(_reasoning_stream())) + list(tr.close()))
    assert not any("reasoning_summary" in e["type"] for e in events)
    assert [item["type"] for item in events[-1]["response"]["output"]] == ["function_call", "message"]
    assert events[-1]["response"]["output_text"] == "答案"


def test_empty_or_redacted_thinking_does_not_create_fake_summary():
    tr = make_stream_translator(CTX)
    events = _events(list(tr.feed(_wire(
        _start(0, {"type": "thinking", "thinking": ""}),
        _delta(0, "signature_delta", signature="signature-only"),
        _stop(0),
        _start(1, {"type": "redacted_thinking", "data": "opaque"}),
        _stop(1),
    ))) + list(tr.close()))
    assert events[-1]["response"]["output"] == []


@pytest.mark.parametrize("mode", ["passthrough", "drop"])
def test_nonstream_thinking_and_native_tool_continuation(mode, monkeypatch):
    monkeypatch.setitem(config.get()["openai"], "reasoningBridge", mode)
    saved = []
    monkeypatch.setattr(store, "is_enabled", lambda: True)
    monkeypatch.setattr(store, "save", lambda **kw: saved.append(kw))
    message = {
        "id": "msg_1", "model": "GLM-5.3", "role": "assistant", "stop_reason": "tool_use",
        "content": [
            {"type": "thinking", "thinking": "先分析", "signature": "signature"},
            {"type": "thinking", "thinking": "再检查"},
            {"type": "redacted_thinking", "data": "opaque"},
            {"type": "text", "text": "正文"},
            {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {"q": "x"}},
        ],
        "usage": {"input_tokens": 5, "output_tokens": 7},
    }
    out = apply_non_stream_response_translator(message, {**CTX, "api_key_name": "test", "current_input_items": []})
    expected_types = (["reasoning", "reasoning"] if mode == "passthrough" else []) + ["message", "function_call"]
    assert [item["type"] for item in out["output"]] == expected_types
    if mode == "passthrough":
        assert [item["summary"] for item in out["output"] if item["type"] == "reasoning"] == [
            [{"type": "summary_text", "text": "先分析"}],
            [{"type": "summary_text", "text": "再检查"}],
        ]
    assert saved[0]["output_items"] == out["output"]
    assert out["output_text"] == "正文"
    assert "encrypted_content" not in json.dumps(out)
    # Unsigned-thinking-compatible upstreams keep summaries typed on replay;
    # signatures and opaque payloads are not invented from readable summaries.
    followup = responses_to_anthropic.translate_request({
        "model": "GLM-5.3",
        "input": [{"role": "user", "content": "查一下"}, *out["output"],
                  {"type": "function_call_output", "call_id": "call_1", "output": "结果"}],
    }, allow_unsigned_thinking=True)
    summaries = ([
        {"type": "thinking", "thinking": "先分析"},
        {"type": "thinking", "thinking": "再检查"},
    ] if mode == "passthrough" else [])
    assert followup["messages"][-2]["content"] == [
        *summaries,
        {"type": "text", "text": "正文"},
        {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {"q": "x"}},
    ]
    assert followup["messages"][-1]["content"] == [
        {"type": "tool_result", "tool_use_id": "call_1", "content": "结果"},
    ]
