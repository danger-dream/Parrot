"""Streaming Anthropic ingress bridge tests (Phase 8 stream slice)."""

from __future__ import annotations

import json

from src.openai.transform.stream_anthropic_to_chat import StreamTranslator as AnthropicToChatStream
from src.openai.transform.stream_anthropic_to_responses import StreamTranslator as AnthropicToResponsesStream
from src.openai.transform.stream_chat_to_anthropic import StreamTranslator as ChatToAnthropicStream
from src.openai.transform.stream_responses_to_anthropic import StreamTranslator as ResponsesToAnthropicStream
from src.protocols.commit_gate import SseCommitGate
from src.protocols.matrix import DEFAULT_MATRIX, extract_request_features


def _chat_chunks(chunks: list[bytes]) -> list[dict | str]:
    out: list[dict | str] = []
    text = b"".join(chunks).decode("utf-8")
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        for line in block.split("\n"):
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            out.append("[DONE]" if data == "[DONE]" else json.loads(data))
    return out


def _events(chunks: list[bytes]) -> list[tuple[str | None, dict]]:
    out: list[tuple[str | None, dict]] = []
    text = b"".join(chunks).decode("utf-8")
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event = None
        data = None
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if data:
            out.append((event, json.loads(data)))
    return out


def _feed_bytewise(translator, payload: bytes) -> list[bytes]:
    """Feed bytes one by one to lock SSE buffering + UTF-8 split behavior."""
    chunks: list[bytes] = []
    for b in payload:
        chunks += list(translator.feed(bytes([b])))
    return chunks


def test_chat_stream_to_anthropic_text_and_usage():
    tr = ChatToAnthropicStream(model="gpt-real")
    chunks: list[bytes] = []
    chunks += list(tr.feed(b'data: {"id":"chatcmpl_1","object":"chat.completion.chunk","created":1,"model":"gpt-real","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'))
    chunks += list(tr.feed(b'data: {"id":"chatcmpl_1","object":"chat.completion.chunk","created":1,"model":"gpt-real","choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n\n'))
    chunks += list(tr.feed(b'data: {"id":"chatcmpl_1","object":"chat.completion.chunk","created":1,"model":"gpt-real","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12,"prompt_tokens_details":{"cached_tokens":3}}}\n\n'))
    chunks += list(tr.feed(b'data: [DONE]\n\n'))
    chunks += list(tr.close())

    events = _events(chunks)
    assert [e for e, _ in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert events[0][1]["message"]["id"] == "chatcmpl_1"
    assert events[2][1]["delta"] == {"type": "text_delta", "text": "hi"}
    assert events[4][1]["delta"]["stop_reason"] == "end_turn"
    assert events[4][1]["usage"] == {
        "input_tokens": 7,
        "output_tokens": 2,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 3,
    }
    assert tr.get_downstream_anthropic_assistant() == {
        "role": "assistant",
        "content": [{"type": "text", "text": "hi"}],
    }


def test_chat_stream_to_anthropic_tool_call():
    tr = ChatToAnthropicStream(model="gpt-real")
    chunks: list[bytes] = []
    chunks += list(tr.feed(b'data: {"id":"chatcmpl_2","object":"chat.completion.chunk","created":1,"model":"gpt-real","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"lookup","arguments":""}}]},"finish_reason":null}]}\n\n'))
    chunks += list(tr.feed(b'data: {"id":"chatcmpl_2","object":"chat.completion.chunk","created":1,"model":"gpt-real","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\\"q\\\":\\\"ping\\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n'))
    chunks += list(tr.feed(b'data: [DONE]\n\n'))
    chunks += list(tr.close())

    events = _events(chunks)
    starts = [d for e, d in events if e == "content_block_start"]
    deltas = [d for e, d in events if e == "content_block_delta"]
    msg_delta = [d for e, d in events if e == "message_delta"][0]
    assert starts[0]["content_block"] == {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {}}
    assert deltas[0]["delta"] == {"type": "input_json_delta", "partial_json": '{"q":"ping"}'}
    assert msg_delta["delta"]["stop_reason"] == "tool_use"
    assert tr.get_downstream_anthropic_assistant()["content"][0] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "lookup",
        "input": {"q": "ping"},
    }


def test_responses_stream_to_anthropic_lazily_emits_after_visible_text():
    tr = ResponsesToAnthropicStream(model="gpt-real")
    gate = SseCommitGate(protocol="anthropic", stream_translator=tr)

    metadata = (
        b'event: response.created\n'
        b'data: {"type":"response.created","response":{"id":"resp_1","status":"in_progress","model":"gpt-real"}}\n\n'
    )
    first = gate.feed(metadata)
    assert first.downstream_chunks == []
    assert first.error_event is None

    visible = (
        b'event: response.output_text.delta\n'
        b'data: {"type":"response.output_text.delta","output_index":0,"content_index":0,"delta":"OK"}\n\n'
    )
    second = gate.feed(visible)
    assert second.error_event is None
    events = _events(second.downstream_chunks)
    assert [e for e, _ in events] == ["message_start", "content_block_start", "content_block_delta"]
    assert events[0][1]["message"]["id"] == "resp_1"
    assert events[2][1]["delta"]["text"] == "OK"

    tail = list(tr.feed(b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_1","status":"completed","model":"gpt-real","usage":{"input_tokens":5,"output_tokens":1,"total_tokens":6}}}\n\n'))
    tail += list(tr.close())
    tail_events = _events(tail)
    assert [e for e, _ in tail_events] == ["content_block_stop", "message_delta", "message_stop"]
    assert tail_events[1][1]["delta"]["stop_reason"] == "end_turn"
    assert tail_events[1][1]["usage"]["input_tokens"] == 5


def test_responses_stream_to_anthropic_tool_call():
    tr = ResponsesToAnthropicStream(model="gpt-real")
    chunks: list[bytes] = []
    chunks += list(tr.feed(b'event: response.output_item.added\ndata: {"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"lookup","arguments":"","status":"in_progress"}}\n\n'))
    chunks += list(tr.feed(b'event: response.function_call_arguments.delta\ndata: {"type":"response.function_call_arguments.delta","output_index":0,"item_id":"fc_1","delta":"{\\\"q\\\":\\\"ping\\\"}"}\n\n'))
    chunks += list(tr.feed(b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_2","status":"completed","model":"gpt-real","usage":{"input_tokens":4,"output_tokens":2,"total_tokens":6}}}\n\n'))
    chunks += list(tr.close())

    events = _events(chunks)
    starts = [d for e, d in events if e == "content_block_start"]
    deltas = [d for e, d in events if e == "content_block_delta"]
    msg_delta = [d for e, d in events if e == "message_delta"][0]
    assert starts[0]["content_block"] == {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {}}
    assert deltas[0]["delta"] == {"type": "input_json_delta", "partial_json": '{"q":"ping"}'}
    assert msg_delta["delta"]["stop_reason"] == "tool_use"


def test_anthropic_stream_to_chat_text_tool_and_usage():
    tr = AnthropicToChatStream(model="claude-real", include_usage=True)
    chunks: list[bytes] = []
    chunks += list(tr.feed(b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant","model":"claude-real","content":[],"usage":{"input_tokens":5,"cache_read_input_tokens":2,"output_tokens":0}}}\n\n'))
    chunks += list(tr.feed(b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'))
    chunks += list(tr.feed(b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"OK"}}\n\n'))
    chunks += list(tr.feed(b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'))
    chunks += list(tr.feed(b'event: content_block_start\ndata: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"call_1","name":"lookup","input":{}}}\n\n'))
    chunks += list(tr.feed(b'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\\"q\\\":\\\"ping\\\"}"}}\n\n'))
    chunks += list(tr.feed(b'event: content_block_stop\ndata: {"type":"content_block_stop","index":1}\n\n'))
    chunks += list(tr.feed(b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},"usage":{"output_tokens":3}}\n\n'))
    chunks += list(tr.feed(b'event: message_stop\ndata: {"type":"message_stop"}\n\n'))
    chunks += list(tr.close())

    items = _chat_chunks(chunks)
    assert items[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert items[1]["choices"][0]["delta"] == {"content": "OK"}
    assert items[2]["choices"][0]["delta"]["tool_calls"][0]["function"] == {
        "name": "lookup", "arguments": '{"q":"ping"}',
    }
    assert items[3]["choices"][0]["finish_reason"] == "tool_calls"
    assert items[4]["choices"] == []
    assert items[4]["usage"] == {
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "total_tokens": 10,
        "prompt_tokens_details": {"cached_tokens": 2},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }
    assert items[-1] == "[DONE]"
    assert tr.get_downstream_chat_assistant()["tool_calls"][0]["id"] == "call_1"


def test_anthropic_stream_to_chat_usage_delta_does_not_zero_prompt_cache():
    tr = AnthropicToChatStream(model="claude-real", include_usage=True)
    chunks: list[bytes] = []
    chunks += list(tr.feed(b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_usage","type":"message","role":"assistant","model":"claude-real","content":[],"usage":{"input_tokens":5,"cache_creation_input_tokens":1,"cache_read_input_tokens":2,"output_tokens":0}}}\n\n'))
    chunks += list(tr.feed(b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'))
    chunks += list(tr.feed(b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"OK"}}\n\n'))
    # Some Anthropic-compatible providers include zero-filled prompt/cache
    # fields on message_delta.  They must not wipe message_start accounting.
    chunks += list(tr.feed(b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":0,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":3}}\n\n'))
    chunks += list(tr.close())

    usage = [item for item in _chat_chunks(chunks) if isinstance(item, dict) and item.get("usage")][-1]["usage"]
    assert usage == {
        "prompt_tokens": 8,
        "completion_tokens": 3,
        "total_tokens": 11,
        "prompt_tokens_details": {"cached_tokens": 2},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }


def test_anthropic_stream_to_responses_text_tool_and_usage():
    tr = AnthropicToResponsesStream(model="claude-real")
    chunks: list[bytes] = []
    chunks += list(tr.feed(b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_2","type":"message","role":"assistant","model":"claude-real","content":[],"usage":{"input_tokens":4,"cache_read_input_tokens":1,"output_tokens":0}}}\n\n'))
    chunks += list(tr.feed(b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'))
    chunks += list(tr.feed(b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"OK"}}\n\n'))
    chunks += list(tr.feed(b'event: content_block_start\ndata: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"call_1","name":"lookup","input":{}}}\n\n'))
    chunks += list(tr.feed(b'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\\"q\\\":\\\"ping\\\"}"}}\n\n'))
    chunks += list(tr.feed(b'event: content_block_stop\ndata: {"type":"content_block_stop","index":1}\n\n'))
    chunks += list(tr.feed(b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},"usage":{"output_tokens":2}}\n\n'))
    chunks += list(tr.close())

    events = _events(chunks)
    names = [e for e, _ in events]
    assert names[:5] == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
    ]
    assert "response.function_call_arguments.delta" in names
    completed = [d for e, d in events if e == "response.completed"][0]["response"]
    assert completed["id"].startswith("resp_")
    assert completed["id"] != "msg_2"
    assert completed["usage"]["input_tokens"] == 5
    assert completed["usage"]["input_tokens_details"] == {"cached_tokens": 1}
    assert completed["output_text"] == "OK"
    assert completed["output"][0]["type"] == "message"
    assert completed["output"][1]["type"] == "function_call"
    assert completed["output"][1]["arguments"] == '{"q":"ping"}'
    assert tr.get_downstream_responses_output()[1]["call_id"] == "call_1"


def test_anthropic_stream_to_responses_usage_delta_does_not_zero_prompt_cache():
    tr = AnthropicToResponsesStream(model="claude-real")
    chunks: list[bytes] = []
    chunks += list(tr.feed(b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_usage_resp","type":"message","role":"assistant","model":"claude-real","content":[],"usage":{"input_tokens":5,"cache_creation_input_tokens":1,"cache_read_input_tokens":2,"output_tokens":0}}}\n\n'))
    chunks += list(tr.feed(b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'))
    chunks += list(tr.feed(b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"OK"}}\n\n'))
    chunks += list(tr.feed(b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":0,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":3}}\n\n'))
    chunks += list(tr.close())

    completed = [d for e, d in _events(chunks) if e == "response.completed"][0]["response"]
    assert completed["usage"] == {
        "input_tokens": 8,
        "input_tokens_details": {"cached_tokens": 2},
        "output_tokens": 3,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 11,
    }


def test_stream_translators_buffer_arbitrary_utf8_chunks():
    text = "你好，Parrot🌙"

    # OpenAI Chat SSE → Anthropic SSE: split inside both JSON and UTF-8 bytes.
    chat_to_anth = ChatToAnthropicStream(model="gpt-real")
    chat_payload = (
        'data: {"id":"chatcmpl_utf8","object":"chat.completion.chunk","created":1,'
        '"model":"gpt-real","choices":[{"index":0,"delta":{"content":"%s"},"finish_reason":null}]}\n\n'
        'data: {"id":"chatcmpl_utf8","object":"chat.completion.chunk","created":1,'
        '"model":"gpt-real","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        'data: [DONE]\n\n'
    ) % text
    chunks = _feed_bytewise(chat_to_anth, chat_payload.encode("utf-8"))
    chunks += list(chat_to_anth.close())
    events = _events(chunks)
    assert any(e == "content_block_delta" and d["delta"]["text"] == text for e, d in events)
    assert chat_to_anth.get_downstream_anthropic_assistant()["content"][0]["text"] == text

    # OpenAI Responses SSE → Anthropic SSE: metadata remains invisible until the
    # first visible text event, even when the SSE frame is arbitrarily split.
    resp_to_anth = ResponsesToAnthropicStream(model="gpt-real")
    resp_payload = (
        'event: response.created\n'
        'data: {"type":"response.created","response":{"id":"resp_utf8","status":"in_progress","model":"gpt-real"}}\n\n'
        'event: response.output_text.delta\n'
        'data: {"type":"response.output_text.delta","output_index":0,"content_index":0,"delta":"%s"}\n\n'
        'event: response.completed\n'
        'data: {"type":"response.completed","response":{"id":"resp_utf8","status":"completed","usage":{"input_tokens":1,"output_tokens":1}}}\n\n'
    ) % text
    chunks = _feed_bytewise(resp_to_anth, resp_payload.encode("utf-8"))
    chunks += list(resp_to_anth.close())
    events = _events(chunks)
    assert events[0][0] == "message_start"
    assert any(e == "content_block_delta" and d["delta"]["text"] == text for e, d in events)
    assert resp_to_anth.get_downstream_anthropic_assistant()["content"][0]["text"] == text

    # Anthropic SSE → OpenAI Chat SSE.
    anth_to_chat = AnthropicToChatStream(model="claude-real", include_usage=True)
    anth_payload = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"id":"msg_utf8","type":"message","role":"assistant","model":"claude-real","content":[],"usage":{"input_tokens":1,"output_tokens":0}}}\n\n'
        'event: content_block_start\n'
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"%s"}}\n\n'
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":1}}\n\n'
        'event: message_stop\n'
        'data: {"type":"message_stop"}\n\n'
    ) % text
    chunks = _feed_bytewise(anth_to_chat, anth_payload.encode("utf-8"))
    chunks += list(anth_to_chat.close())
    items = _chat_chunks(chunks)
    first_obj = next(item for item in items if isinstance(item, dict) and item.get("object") == "chat.completion.chunk")
    assert first_obj["id"].startswith("chatcmpl-")
    assert first_obj["id"] != "msg_utf8"
    assert any(isinstance(item, dict) and item["choices"] and item["choices"][0]["delta"].get("content") == text for item in items)
    assert anth_to_chat.get_downstream_chat_assistant()["content"] == text

    # Anthropic SSE → OpenAI Responses SSE.
    anth_to_resp = AnthropicToResponsesStream(model="claude-real")
    chunks = _feed_bytewise(anth_to_resp, anth_payload.encode("utf-8"))
    chunks += list(anth_to_resp.close())
    events = _events(chunks)
    created = [d for e, d in events if e == "response.created"][0]["response"]
    assert created["id"].startswith("resp_")
    assert created["id"] != "msg_utf8"
    assert any(e == "response.output_text.delta" and d["delta"] == text for e, d in events)
    assert anth_to_resp.get_downstream_responses_output()[0]["content"][0]["text"] == text


def test_stream_tool_arguments_accumulate_across_protocol_chunks():
    # OpenAI Chat SSE → Anthropic SSE: tool arguments arrive as two deltas but
    # downstream assistant state must keep valid full JSON.
    chat_to_anth = ChatToAnthropicStream(model="gpt-real")
    chunks: list[bytes] = []
    chunks += list(chat_to_anth.feed(b'data: {"id":"chatcmpl_split","object":"chat.completion.chunk","created":1,"model":"gpt-real","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_split","type":"function","function":{"name":"lookup","arguments":"{\\"q\\""}}]},"finish_reason":null}]}\n\n'))
    chunks += list(chat_to_anth.feed(b'data: {"id":"chatcmpl_split","object":"chat.completion.chunk","created":1,"model":"gpt-real","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":":\\"ping\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n'))
    chunks += list(chat_to_anth.close())
    events = _events(chunks)
    deltas = [d for e, d in events if e == "content_block_delta"]
    assert [d["delta"]["partial_json"] for d in deltas] == ['{"q"', ':"ping"}']
    assert chat_to_anth.get_downstream_anthropic_assistant()["content"][0]["input"] == {"q": "ping"}

    # OpenAI Responses SSE → Anthropic SSE.
    resp_to_anth = ResponsesToAnthropicStream(model="gpt-real")
    chunks = []
    chunks += list(resp_to_anth.feed(b'event: response.output_item.added\ndata: {"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"fc_split","call_id":"call_split","name":"lookup","arguments":"","status":"in_progress"}}\n\n'))
    chunks += list(resp_to_anth.feed(b'event: response.function_call_arguments.delta\ndata: {"type":"response.function_call_arguments.delta","output_index":0,"item_id":"fc_split","delta":"{\\"q\\""}\n\n'))
    chunks += list(resp_to_anth.feed(b'event: response.function_call_arguments.delta\ndata: {"type":"response.function_call_arguments.delta","output_index":0,"item_id":"fc_split","delta":":\\"ping\\"}"}\n\n'))
    chunks += list(resp_to_anth.close())
    events = _events(chunks)
    deltas = [d for e, d in events if e == "content_block_delta"]
    assert [d["delta"]["partial_json"] for d in deltas] == ['{"q"', ':"ping"}']
    assert resp_to_anth.get_downstream_anthropic_assistant()["content"][0]["input"] == {"q": "ping"}

    # Anthropic SSE → OpenAI Chat SSE: Anthropic partial_json chunks are emitted
    # as one complete Chat tool_calls delta at content_block_stop.
    anth_to_chat = AnthropicToChatStream(model="claude-real")
    chunks = []
    chunks += list(anth_to_chat.feed(b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"call_split","name":"lookup","input":{}}}\n\n'))
    chunks += list(anth_to_chat.feed(b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"q\\""}}\n\n'))
    chunks += list(anth_to_chat.feed(b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":":\\"ping\\"}"}}\n\n'))
    chunks += list(anth_to_chat.feed(b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'))
    chunks += list(anth_to_chat.close())
    items = _chat_chunks(chunks)
    tool_delta = next(item for item in items if isinstance(item, dict) and item["choices"] and item["choices"][0]["delta"].get("tool_calls"))
    assert tool_delta["choices"][0]["delta"]["tool_calls"][0]["function"] == {
        "name": "lookup",
        "arguments": '{"q":"ping"}',
    }
    assert anth_to_chat.get_downstream_chat_assistant()["tool_calls"][0]["function"]["arguments"] == '{"q":"ping"}'

    # Anthropic SSE → OpenAI Responses SSE.
    anth_to_resp = AnthropicToResponsesStream(model="claude-real")
    chunks = []
    chunks += list(anth_to_resp.feed(b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"call_split","name":"lookup","input":{}}}\n\n'))
    chunks += list(anth_to_resp.feed(b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"q\\""}}\n\n'))
    chunks += list(anth_to_resp.feed(b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":":\\"ping\\"}"}}\n\n'))
    chunks += list(anth_to_resp.feed(b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'))
    chunks += list(anth_to_resp.close())
    events = _events(chunks)
    arg_deltas = [d for e, d in events if e == "response.function_call_arguments.delta"]
    assert [d["delta"] for d in arg_deltas] == ['{"q"', ':"ping"}']
    assert anth_to_resp.get_downstream_responses_output()[0]["arguments"] == '{"q":"ping"}'


def test_matrix_allows_anthropic_stream_to_openai_stream_upstreams():
    body = {"stream": True, "messages": [{"role": "user", "content": "hi"}]}
    features = extract_request_features("anthropic", body)
    assert DEFAULT_MATRIX.plan("anthropic", "openai-chat", features=features).required_transforms == ["anthropic_to_chat"]
    assert DEFAULT_MATRIX.plan("anthropic", "openai-responses", features=features).required_transforms == ["anthropic_to_responses"]


def test_matrix_allows_openai_stream_to_anthropic_stream_upstream():
    chat_body = {"stream": True, "messages": [{"role": "user", "content": "hi"}]}
    resp_body = {"stream": True, "input": "hi"}
    assert DEFAULT_MATRIX.plan("chat", "anthropic", features=extract_request_features("chat", chat_body)).required_transforms == ["chat_to_anthropic"]
    assert DEFAULT_MATRIX.plan("responses", "anthropic", features=extract_request_features("responses", resp_body)).required_transforms == ["responses_to_anthropic"]
