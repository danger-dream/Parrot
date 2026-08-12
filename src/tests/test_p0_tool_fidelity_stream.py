"""P0 Responses tool-call stream → Chat regression tests."""
from __future__ import annotations

import json
import os as _ap_os, sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

from src.openai.transform import stream_r2c


def _event(name: str, data: dict) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()


def _chat_payloads(chunks: list[bytes]) -> list[dict]:
    result = []
    for block in b"".join(chunks).decode().split("\n\n"):
        if block.startswith("data: ") and block != "data: [DONE]":
            result.append(json.loads(block[6:]))
    return result


def _tool_deltas(payloads: list[dict]) -> list[dict]:
    out = []
    for payload in payloads:
        for choice in payload.get("choices") or []:
            out.extend(choice.get("delta", {}).get("tool_calls") or [])
    return out


def test_custom_stream_delta_done_and_item_done_emit_suffix_once():
    tr = stream_r2c.StreamTranslator(model="x")
    chunks = []
    chunks.extend(tr.feed(_event("response.output_item.added", {
        "output_index": 0, "item": {"type": "custom_tool_call", "call_id": "custom_1", "name": "dsl", "input": ""},
    })))
    chunks.extend(tr.feed(_event("response.custom_tool_call_input.delta", {"output_index": 0, "delta": "raw"})))
    chunks.extend(tr.feed(_event("response.custom_tool_call_input.done", {"output_index": 0, "input": "raw input"})))
    chunks.extend(tr.feed(_event("response.output_item.done", {
        "output_index": 0, "item": {"type": "custom_tool_call", "call_id": "custom_1", "name": "dsl", "input": "raw input"},
    })))
    chunks.extend(tr.feed(_event("response.completed", {"response": {"status": "completed", "output": []}})))
    chunks.extend(tr.close())
    deltas = _tool_deltas(_chat_payloads(chunks))
    assert deltas[0] == {"index": 0, "id": "custom_1", "type": "custom", "custom": {"name": "dsl", "input": ""}}
    assert "".join(d.get("custom", {}).get("input", "") for d in deltas[1:]) == "raw input"
    assert tr.get_downstream_chat_assistant()["tool_calls"][0]["custom"]["input"] == "raw input"


def test_function_and_custom_share_stable_indices_and_ids():
    tr = stream_r2c.StreamTranslator(model="x")
    chunks = []
    chunks.extend(tr.feed(_event("response.output_item.added", {
        "output_index": 4, "item": {"type": "function_call", "call_id": "function_1", "name": "fn", "arguments": ""},
    })))
    chunks.extend(tr.feed(_event("response.output_item.added", {
        "output_index": 9, "item": {"type": "custom_tool_call", "call_id": "custom_1", "name": "dsl", "input": ""},
    })))
    chunks.extend(tr.feed(_event("response.function_call_arguments.done", {"output_index": 4, "arguments": "{}"})))
    chunks.extend(tr.feed(_event("response.custom_tool_call_input.done", {"output_index": 9, "input": "raw"})))
    starts = [d for d in _tool_deltas(_chat_payloads(chunks)) if "id" in d]
    assert [(d["index"], d["id"], d["type"]) for d in starts] == [
        (0, "function_1", "function"), (1, "custom_1", "custom")]


def test_custom_terminal_snapshot_fallback_emits_complete_input():
    tr = stream_r2c.StreamTranslator(model="x")
    chunks = list(tr.feed(_event("response.completed", {"response": {
        "status": "completed", "output": [{"type": "custom_tool_call", "call_id": "custom_1", "name": "dsl", "input": "snapshot"}],
    }})))
    chunks.extend(tr.close())
    payloads = _chat_payloads(chunks)
    deltas = _tool_deltas(payloads)
    assert deltas[0]["id"] == "custom_1"
    assert "".join(d.get("custom", {}).get("input", "") for d in deltas) == "snapshot"
    assert any(c["finish_reason"] == "tool_calls" for p in payloads for c in p.get("choices") or [])


def test_function_terminal_snapshot_fallback_emits_complete_call():
    tr = stream_r2c.StreamTranslator(model="x")
    chunks = list(tr.feed(_event("response.completed", {"response": {
        "status": "completed", "output": [{
            "type": "function_call", "call_id": "function_1",
            "name": "lookup", "arguments": '{"city":"Paris"}',
        }],
    }})))
    chunks.extend(tr.close())
    payloads = _chat_payloads(chunks)
    deltas = _tool_deltas(payloads)
    assert deltas[0] == {
        "index": 0, "id": "function_1", "type": "function",
        "function": {"name": "lookup", "arguments": ""},
    }
    assert "".join(d.get("function", {}).get("arguments", "") for d in deltas) == '{"city":"Paris"}'
    assert tr.get_downstream_chat_assistant()["tool_calls"][0] == {
        "id": "function_1", "type": "function",
        "function": {"name": "lookup", "arguments": '{"city":"Paris"}'},
    }
    assert any(c["finish_reason"] == "tool_calls" for p in payloads for c in p.get("choices") or [])


def test_function_delta_then_terminal_snapshot_emits_only_arguments_suffix():
    tr = stream_r2c.StreamTranslator(model="x")
    chunks = []
    chunks.extend(tr.feed(_event("response.output_item.added", {
        "output_index": 7, "item": {
            "type": "function_call", "call_id": "function_1", "name": "lookup", "arguments": "",
        },
    })))
    chunks.extend(tr.feed(_event("response.function_call_arguments.delta", {
        "output_index": 7, "delta": '{"city":',
    })))
    chunks.extend(tr.feed(_event("response.completed", {"response": {
        "status": "completed", "output": [{
            "type": "function_call", "call_id": "function_1",
            "name": "lookup", "arguments": '{"city":"Paris"}',
        }],
    }})))
    chunks.extend(tr.close())
    deltas = _tool_deltas(_chat_payloads(chunks))
    starts = [d for d in deltas if "id" in d]
    assert [(d["index"], d["id"]) for d in starts] == [(0, "function_1")]
    assert "".join(d.get("function", {}).get("arguments", "") for d in deltas) == '{"city":"Paris"}'


def test_mixed_terminal_snapshot_preserves_order_indices_ids_and_payloads():
    tr = stream_r2c.StreamTranslator(model="x")
    chunks = list(tr.feed(_event("response.completed", {"response": {
        "status": "completed", "output": [
            {"type": "custom_tool_call", "call_id": "custom_1", "name": "dsl", "input": "raw"},
            {"type": "function_call", "call_id": "function_1", "name": "lookup", "arguments": "{}"},
        ],
    }})))
    chunks.extend(tr.close())
    deltas = _tool_deltas(_chat_payloads(chunks))
    starts = [d for d in deltas if "id" in d]
    assert [(d["index"], d["id"], d["type"]) for d in starts] == [
        (0, "custom_1", "custom"), (1, "function_1", "function"),
    ]
    assert "".join(d.get("custom", {}).get("input", "") for d in deltas if d["index"] == 0) == "raw"
    assert "".join(d.get("function", {}).get("arguments", "") for d in deltas if d["index"] == 1) == "{}"
