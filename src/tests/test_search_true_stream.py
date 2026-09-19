"""Managed search: arrival ordering, not eventual string containment."""
import asyncio
import copy
import json
import time

import pytest
from fastapi.responses import StreamingResponse

from src import local_web_tools as web, search_tool_policy as policy
from src.tests.test_search_tool_policy import request, reply, settings  # noqa: F401
from src.tests.search_stream_fixtures import events, encode, decode, text_delta


def with_text(obj, protocol, text):
    obj = copy.deepcopy(obj)
    if protocol == "responses":
        obj["output"].insert(0, {"type": "message", "id": "same-message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text}]})
    elif protocol == "anthropic":
        obj["content"].insert(0, {"type": "text", "text": text})
    else:
        obj["choices"][0]["message"]["content"] = text
    return obj


async def collect(response, on_data=None):
    raw = b""
    async for chunk in response.body_iterator:
        raw += chunk
        if on_data:
            for data in decode(chunk):
                on_data(data)
    return raw, decode(raw)


@pytest.mark.parametrize("protocol", ["responses", "chat", "anthropic"])
@pytest.mark.parametrize("search_rounds", [0, 1, 2])
async def test_text_arrives_before_each_upstream_terminal(protocol, search_rounds, monkeypatch):
    body = request(protocol, hosted=True)
    body["stream_options"] = {"include_usage": True}
    name = next(iter(policy.compile_request(body, protocol)[1].values())).name
    invoked, searches, released, seen, marks = [], [], [], [], []
    gates = [asyncio.Event() for _ in range(search_rounds + 1)]
    async def execute(calls, **kw):
        searches.extend(calls)
        return [web.LocalToolResult(c.id, "private search result") for c in calls]
    monkeypatch.setattr(web, "execute_local_tool_calls", execute)
    async def invoke(current):
        assert current["stream"] is True
        number = len(invoked)
        invoked.append(copy.deepcopy(current))
        obj = reply(protocol, name if number < search_rounds else None)
        if number < search_rounds:
            # Distinct call ids; providers may reuse their item ids across rounds.
            for entry in policy._calls(obj, protocol):
                entry[0]["call_id" if protocol == "responses" else "id"] = f"search-{number}"
            obj = with_text(obj, protocol, "我查一下")
        async def source():
            try:
                count = 0
                for event, data in events(obj, protocol):
                    if data and text_delta(data, protocol):
                        count += 1
                    yield encode(event, data)
                    if count == 2:
                        # The provider cannot finish until two actual text
                        # deltas have reached the client. Buffering deadlocks.
                        await asyncio.wait_for(gates[number].wait(), 1)
                        count += 1
                    await asyncio.sleep(0)
                marks.append((number, "upstream-ended", time.monotonic()))
            finally:
                released.append(number)
        return StreamingResponse(source(), media_type="text/event-stream")
    def receive(data):
        delta = text_delta(data, protocol)
        if delta:
            number = len(invoked) - 1
            seen.append((number, delta))
            marks.append((number, "downstream-text", time.monotonic()))
            if len([x for x in seen if x[0] == number]) >= 2:
                assert number not in released
                gates[number].set()
    raw, frames = await asyncio.wait_for(collect(policy.stream(body, protocol, invoke), receive), 4)
    assert len(invoked) == search_rounds + 1 and len(searches) == search_rounds
    assert "".join(x[1] for x in seen) == "我查一下" * search_rounds + "Found Python docs"
    assert b"parrot_hosted_" not in raw and b"private search result" not in raw
    if protocol == "responses":
        terminal = [x for x in frames if x.get("type") == "response.completed"]
        assert len(terminal) == 1
        final = terminal[0]["response"]
        assert final["id"] == frames[0]["response"]["id"]
        assert len(final["output"]) == search_rounds + 1
        assert len({x["id"] for x in final["output"]}) == len(final["output"])
        assert [x["sequence_number"] for x in frames] == list(range(len(frames)))
        assert final["usage"]["input_tokens"] == 12 * (search_rounds + 1)
    elif protocol == "anthropic":
        assert len([x for x in frames if x.get("type") == "message_start"]) == 1
        assert len([x for x in frames if x.get("type") == "message_stop"]) == 1
        assert [x["index"] for x in frames if x.get("type") == "content_block_start"] == list(range(search_rounds + 1))
        assert next(x["usage"] for x in frames if x.get("type") == "message_delta")["input_tokens"] == 12 * (search_rounds + 1)
    else:
        assert raw.count(b"[DONE]") == 1
        assert len([c for x in frames for c in x.get("choices", []) if c.get("finish_reason")]) == 1
        assert next(x["usage"] for x in frames if "usage" in x)["prompt_tokens"] == 12 * (search_rounds + 1)


@pytest.mark.parametrize("protocol", ["responses", "chat", "anthropic"])
async def test_mixed_replay_includes_already_streamed_preamble_without_reexecution(protocol, monkeypatch):
    body = request(protocol, hosted=True, mixed=True)
    name = next(iter(policy.compile_request(body, protocol)[1].values())).name
    searches = []
    async def execute(calls, **kw):
        searches.extend(calls)
        return [web.LocalToolResult(c.id, "hidden result") for c in calls]
    monkeypatch.setattr(web, "execute_local_tool_calls", execute)
    async def invoke(current):
        obj = with_text(reply(protocol, name, mixed=True), protocol, "我查一下")
        async def source():
            for event, data in events(obj, protocol):
                yield encode(event, data)
        return StreamingResponse(source())
    raw, frames = await collect(policy.stream(body, protocol, invoke, api_key_name="k"))
    assert b"parrot_hosted_" not in raw and len(searches) == 1
    if protocol == "responses":
        visible = frames[-1]["response"]
        continuation = {"model": body["model"], "previous_response_id": visible["id"], "input": [{"type": "function_call_output", "call_id": "client-1", "output": "client result"}]}
    elif protocol == "anthropic":
        builder = __import__("src.upstream", fromlist=["SSEAssistantBuilder"]).SSEAssistantBuilder()
        builder.feed(raw)
        visible = builder.get_assistant()
        continuation = copy.deepcopy(body)
        # Clients send assistant+user result, not an empty internal result turn.
        continuation["messages"].append(visible)
        continuation["messages"].append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "client-1", "content": "client result"}]})
    else:
        from src.transports.chat_aggregate import ChatAggregateBuilder
        builder = ChatAggregateBuilder()
        builder.feed(raw)
        visible = builder.to_full_json()
        continuation = copy.deepcopy(body)
        policy._append(continuation, visible, [], protocol)
        continuation["messages"].append({"role": "tool", "tool_call_id": "client-1", "content": "client result"})
    restored = policy.restore_replay(continuation, protocol, "k")
    history = json.dumps(policy._history(restored, protocol), ensure_ascii=False)
    assert history.count("hidden result") == 1 and history.count("我查一下") == 1
    assert "client result" in history
    resumed = []
    async def continuation_model(current):
        resumed.append(copy.deepcopy(current))
        async def source():
            for event, data in events(reply(protocol), protocol):
                yield encode(event, data)
        return StreamingResponse(source())
    _, next_frames = await collect(policy.stream(continuation, protocol, continuation_model, api_key_name="k"))
    assert len(searches) == 1 and len(resumed) == 1
    assert "".join(text_delta(f, protocol) for f in next_frames) == "Found Python docs"
    assert json.dumps(policy._history(resumed[0], protocol), ensure_ascii=False).count("我查一下") == 1


async def test_chat_independent_choices_fragmented_names_and_legacy(monkeypatch):
    body = request("chat", hosted=True)
    body["n"] = 2
    name = next(iter(policy.compile_request(body, "chat")[1].values())).name
    calls, searches = [], []
    async def execute(batch, **kw):
        searches.extend(batch)
        return [web.LocalToolResult(c.id, "result") for c in batch]
    monkeypatch.setattr(web, "execute_local_tool_calls", execute)
    async def invoke(current):
        calls.append(copy.deepcopy(current))
        if len(calls) == 1:
            obj = with_text(reply("chat", name), "chat", "A")
            obj["choices"] += [{"index": 1, "message": {"role": "assistant", "content": "B", "function_call": {"name": name, "arguments": '{"query":"B"}'}}, "finish_reason": "function_call"}]
        else:
            obj = reply("chat")
            obj["choices"][0]["message"]["content"] = str(len(calls))
        async def source():
            for event, data in events(obj, "chat"):
                yield encode(event, data)
        return StreamingResponse(source())
    raw, frames = await collect(policy.stream(body, "chat", invoke))
    assert len(calls) == 3 and len(searches) == 2
    assert all(x["n"] == 1 for x in calls[1:])
    assert b"parrot_hosted_" not in raw
    texts = {i: "".join(c.get("delta", {}).get("content") or "" for f in frames for c in f.get("choices", []) if c["index"] == i) for i in (0, 1)}
    assert texts == {0: "A2", 1: "B3"}
    assert len([c for f in frames for c in f.get("choices", []) if c.get("finish_reason")]) == 2


@pytest.mark.parametrize("protocol", ["responses", "chat", "anthropic"])
async def test_cancel_visible_stream_closes_upstream(protocol):
    closed = asyncio.Event()
    async def source():
        try:
            for event, data in events(reply(protocol), protocol):
                yield encode(event, data)
                if data and text_delta(data, protocol):
                    await asyncio.Event().wait()
        finally:
            closed.set()
    async def invoke(_):
        return StreamingResponse(source())
    response = policy.stream(request(protocol), protocol, invoke)
    iterator = response.body_iterator
    while True:
        chunk = await asyncio.wait_for(anext(iterator), 1)
        if any(text_delta(d, protocol) for d in decode(chunk)):
            break
    await iterator.aclose()
    assert closed.is_set()
