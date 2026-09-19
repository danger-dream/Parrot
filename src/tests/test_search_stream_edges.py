"""Targeted stream failure, tool ownership and budget boundaries."""
import asyncio
import copy
import json

import pytest
from fastapi.responses import StreamingResponse

from src import local_web_tools as web, search_tool_policy as policy
from src.tests.test_search_tool_policy import request, reply, settings  # noqa: F401
from src.tests.test_search_true_stream import collect, with_text
from src.tests.search_stream_fixtures import events, encode, text_delta


@pytest.mark.parametrize("protocol", ["responses", "chat", "anthropic"])
@pytest.mark.parametrize("failure", ["eof", "error", "exception"])
async def test_visible_text_never_retried_or_completed_after_failure(protocol, failure):
    invocations, closed = [], []
    async def invoke(body):
        invocations.append(body)
        async def source():
            try:
                count = 0
                for event, data in events(reply(protocol), protocol):
                    yield encode(event, data)
                    if data and text_delta(data, protocol):
                        count += 1
                        if count == 2:
                            break
                if failure == "error":
                    yield encode("error", {"type": "error", "error": {"code": "broken", "message": "broken"}})
                elif failure == "exception":
                    raise IOError("connection broken")
            finally:
                closed.append(True)
        return StreamingResponse(source())
    raw, frames = await collect(policy.stream(request(protocol), protocol, invoke))
    assert len(invocations) == 1 and closed == [True]
    assert "".join(text_delta(f, protocol) for f in frames) == "Fo"
    assert b"[DONE]" not in raw
    assert not any(f.get("type") in ("response.completed", "message_stop") for f in frames)
    if protocol == "responses":
        failed = frames[-1]
        assert failed["type"] == "response.failed"
        assert failed["response"]["output"][0]["content"][0]["text"] == "Fo"
        assert failed["response"]["id"] == frames[0]["response"]["id"]
    else:
        assert frames[-1]["type"] == "error"


@pytest.mark.parametrize("protocol", ["responses", "chat", "anthropic"])
async def test_declared_budget_shared_across_incremental_rounds(protocol, monkeypatch):
    body = request(protocol, hosted=True)
    body["tools"][0]["max_uses"] = 1
    name = next(iter(policy.compile_request(body, protocol)[1].values())).name
    invocations, executed = [], []
    async def execute(calls, **kw):
        executed.extend(calls)
        return [web.LocalToolResult(c.id, "search result") for c in calls]
    monkeypatch.setattr(web, "execute_local_tool_calls", execute)
    async def invoke(current):
        n = len(invocations)
        invocations.append(copy.deepcopy(current))
        obj = reply(protocol, name if n < 2 else None)
        for item, *_ in policy._calls(obj, protocol):
            item["call_id" if protocol == "responses" else "id"] = "search-" + str(n)
        async def source():
            for event, data in events(obj, protocol):
                yield encode(event, data)
        return StreamingResponse(source())
    _, frames = await collect(policy.stream(body, protocol, invoke))
    assert len(invocations) == 3 and len(executed) == 1
    assert "max_uses_exceeded" in json.dumps(invocations[-1])
    assert "".join(text_delta(f, protocol) for f in frames) == "Found Python docs"


@pytest.mark.parametrize("protocol", ["responses", "chat", "anthropic"])
@pytest.mark.parametrize("conflict", [False, True])
async def test_duplicate_external_calls_are_not_emitted_twice(protocol, conflict):
    obj = reply(protocol, mixed=True)
    if protocol == "responses":
        calls = obj["output"]
    elif protocol == "anthropic":
        calls = obj["content"]
    else:
        calls = obj["choices"][0]["message"]["tool_calls"]
    duplicate = copy.deepcopy(calls[0])
    if conflict:
        if protocol == "chat":
            duplicate["function"]["arguments"] = '{"query":"conflicting"}'
        elif protocol == "responses":
            duplicate["arguments"] = '{"query":"conflicting"}'
        else:
            duplicate["input"] = {"query": "conflicting"}
    calls.append(duplicate)
    async def invoke(_):
        async def source():
            for event, data in events(obj, protocol):
                yield encode(event, data)
        return StreamingResponse(source())
    raw, frames = await collect(policy.stream(request(protocol, mixed=True), protocol, invoke))
    if conflict:
        assert b"tool_call_id_conflict" in raw
    elif protocol == "responses":
        assert len(frames[-1]["response"]["output"]) == 1
        assert len([f for f in frames if f.get("type") == "response.output_item.added"]) == 1
    elif protocol == "anthropic":
        assert len([f for f in frames if f.get("type") == "content_block_start"]) == 1
    else:
        assert len([t for f in frames for c in f.get("choices", []) for t in c.get("delta", {}).get("tool_calls", [])]) == 1


async def test_responses_late_tool_name_hidden_with_byte_fragmentation():
    body = request(hosted=True)
    name = next(iter(policy.compile_request(body, "responses")[1].values())).name
    # max_uses=0 avoids external execution and proves the reconstructed name
    # was recognized, rather than being exposed as a client function.
    body["tools"][0]["max_uses"] = 0
    invoked = []
    async def invoke(current):
        invoked.append(copy.deepcopy(current))
        obj = reply("responses", name if len(invoked) == 1 else None)
        async def source():
            for event, data in events(obj, "responses"):
                if event == "response.output_item.added" and data["item"]["type"] == "function_call":
                    data["item"]["name"] = ""
                raw = encode(event, data).replace(b"\n", b"\r\n")
                for i in range(0, len(raw), 3):
                    yield raw[i:i+3]
        return StreamingResponse(source())
    raw, frames = await collect(policy.stream(body, "responses", invoke))
    assert len(invoked) == 2
    assert "max_uses_exceeded" in json.dumps(invoked[1])
    assert b"parrot_hosted" not in raw
    assert "".join(text_delta(f, "responses") for f in frames) == "Found Python docs"


async def test_mixed_terminal_waits_for_managed_execution(monkeypatch):
    body = request(hosted=True, mixed=True)
    name = next(iter(policy.compile_request(body, "responses")[1].values())).name
    entered, release = asyncio.Event(), asyncio.Event()
    async def execute(calls, **kw):
        entered.set()
        await release.wait()
        return [web.LocalToolResult(c.id, "private result") for c in calls]
    monkeypatch.setattr(web, "execute_local_tool_calls", execute)
    async def invoke(_):
        async def source():
            for event, data in events(with_text(reply("responses", name, mixed=True), "responses", "before"), "responses"):
                yield encode(event, data)
        return StreamingResponse(source())
    received = []
    response = policy.stream(body, "responses", invoke)
    task = asyncio.create_task(collect(response, received.append))
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.sleep(0)
    assert "".join(text_delta(f, "responses") for f in received) == "before"
    assert not any(f.get("type") == "response.completed" for f in received)
    assert "client-1" not in json.dumps(received)
    release.set()
    _, frames = await asyncio.wait_for(task, 1)
    assert frames[-1]["type"] == "response.completed"
    assert [c[1] for c in policy._calls(frames[-1]["response"], "responses")] == ["client-1"]


async def test_incomplete_keeps_partial_output_id_details_and_cumulative_usage(monkeypatch):
    body = request(hosted=True)
    name = next(iter(policy.compile_request(body, "responses")[1].values())).name
    invoked = []
    async def execute(calls, **kw):
        return [web.LocalToolResult(c.id, "result") for c in calls]
    monkeypatch.setattr(web, "execute_local_tool_calls", execute)
    async def invoke(current):
        invoked.append(current)
        number = len(invoked)
        obj = with_text(reply("responses", name), "responses", "before") if number == 1 else reply("responses")
        async def source():
            for event, data in events(obj, "responses"):
                if number == 2 and event == "response.completed":
                    event = data["type"] = "response.incomplete"
                    data["response"].update(status="incomplete", incomplete_details={"reason": "max_output_tokens"},
                                            usage={"input_tokens": 3, "output_tokens": 1})
                yield encode(event, data)
        return StreamingResponse(source())
    _, frames = await collect(policy.stream(body, "responses", invoke))
    assert len(invoked) == 2
    assert frames[-1]["type"] == "response.incomplete"
    final = frames[-1]["response"]
    assert final["id"] == frames[0]["response"]["id"]
    assert final["incomplete_details"] == {"reason": "max_output_tokens"}
    assert final["usage"] == {"input_tokens": 15, "output_tokens": 6}
    assert "".join(part["text"] for item in final["output"] for part in item.get("content", [])) == "beforeFound Python docs"


async def test_anthropic_invalid_streamed_json_is_not_executed(monkeypatch):
    body = request("anthropic", hosted=True)
    name = next(iter(policy.compile_request(body, "anthropic")[1].values())).name
    invoked = []
    async def execute(calls, **kw):
        assert calls == []
        return []
    monkeypatch.setattr(web, "execute_local_tool_calls", execute)
    async def invoke(current):
        invoked.append(copy.deepcopy(current))
        obj = reply("anthropic", name if len(invoked) == 1 else None)
        async def source():
            for event, data in events(obj, "anthropic"):
                if (data.get("delta") or {}).get("type") == "input_json_delta":
                    data["delta"]["partial_json"] = "["
                yield encode(event, data)
        return StreamingResponse(source())
    _, frames = await collect(policy.stream(body, "anthropic", invoke))
    assert len(invoked) == 2 and "invalid_input" in json.dumps(invoked[1])
    assert "".join(text_delta(f, "anthropic") for f in frames) == "Found Python docs"
