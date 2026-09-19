"""Sparse Responses snapshots must reach incremental clients exactly once."""
import copy
import json

import pytest
from fastapi.responses import StreamingResponse

from src import local_web_tools as web, search_tool_policy as policy
from src.tests.test_search_tool_policy import request, reply, settings  # noqa: F401
from src.tests.search_stream_fixtures import events, encode, text_delta
from src.tests.test_search_true_stream import collect, with_text


@pytest.mark.parametrize("mode", [
    "normal", "terminal", "partial_terminal", "item_done", "part_done",
    "text_done", "item_added", "terminal_only",
])
async def test_sparse_message_snapshots_complete_incremental_lifecycle(mode):
    obj = reply("responses")
    expected = obj["output"][0]["content"][0]["text"]
    permitted_done = {
        "normal": {"response.output_text.done", "response.content_part.done", "response.output_item.done"},
        "item_done": {"response.output_item.done"},
        "item_added": {"response.output_item.done"},
        "part_done": {"response.content_part.done"},
        "text_done": {"response.output_text.done"},
    }.get(mode, set())

    async def invoke(_):
        async def source():
            delta_count = 0
            for event, data in events(obj, "responses"):
                if mode == "terminal_only" and event not in ("response.created", "response.completed"):
                    continue
                if event == "response.output_text.delta":
                    delta_count += 1
                    if mode != "normal" and not (mode == "partial_terminal" and delta_count <= 2):
                        continue
                if event.endswith(".done") and event not in permitted_done:
                    continue
                if mode == "item_added" and event == "response.output_item.added":
                    data["item"] = copy.deepcopy(obj["output"][0])
                yield encode(event, data)
        return StreamingResponse(source())

    _, frames = await collect(policy.stream(request(), "responses", invoke))
    assert "".join(text_delta(f, "responses") for f in frames) == expected
    types = [f["type"] for f in frames]
    ordered = ["response.output_item.added", "response.content_part.added",
               "response.output_text.delta", "response.output_text.done",
               "response.content_part.done", "response.output_item.done", "response.completed"]
    assert [types.index(t) for t in ordered] == sorted(types.index(t) for t in ordered)
    assert all(types.count(t) == 1 for t in ordered if not t.endswith(".delta"))
    assert [f["item"] for f in frames if f["type"] == "response.output_item.done"] == frames[-1]["response"]["output"]
    # A real SDK can build from starts + deltas without also adding full text
    # embedded in an item/part start.
    assert next(f["item"]["content"] for f in frames if f["type"] == "response.output_item.added") == []
    assert next(f["part"]["text"] for f in frames if f["type"] == "response.content_part.added") == ""


async def test_terminal_only_reasoning_and_mixed_calls_keep_full_history_replay(monkeypatch):
    body = request(hosted=True, mixed=True)
    body["input"] = [{"role": "user", "content": "search Python"}]
    name = next(iter(policy.compile_request(body, "responses")[1].values())).name
    obj = with_text(reply("responses", name, mixed=True), "responses", "我查一下")
    opaque = {"type": "reasoning", "id": "rs-native", "summary": [], "encrypted_content": "opaque-test-value"}
    obj["output"].insert(0, opaque)
    searches = []

    async def execute(calls, **kw):
        searches.extend(calls)
        return [web.LocalToolResult(c.id, "hidden result") for c in calls]
    monkeypatch.setattr(web, "execute_local_tool_calls", execute)

    async def invoke(_):
        async def source():
            yield encode("response.created", {"type": "response.created", "response": {**obj, "output": [], "status": "in_progress"}})
            yield encode("response.output_item.added", {"type": "response.output_item.added", "output_index": 0,
                                                       "item": {"type": "reasoning", "id": "rs-native", "summary": []}})
            yield encode("response.completed", {"type": "response.completed", "response": obj})
        return StreamingResponse(source())

    raw, frames = await collect(policy.stream(body, "responses", invoke, api_key_name="snapshot-test"))
    assert b"parrot_hosted_" not in raw and b"hidden result" not in raw
    assert len(searches) == 1
    done = [f["item"] for f in frames if f["type"] == "response.output_item.done"]
    assert done == frames[-1]["response"]["output"]
    assert [item for item in done if item["type"] == "reasoning"] == [opaque]
    assert "".join(text_delta(f, "responses") for f in frames) == "我查一下"
    external = next(item for item in done if item["type"] == "function_call")
    args = "".join(f["delta"] for f in frames if f["type"] == "response.function_call_arguments.delta")
    assert args == external["arguments"]
    assert next(f["item"]["arguments"] for f in frames if f["type"] == "response.output_item.added" and f["item"]["type"] == "function_call") == ""
    continuation = copy.deepcopy(body)
    continuation["input"] += done + [{"type": "function_call_output", "call_id": "client-1", "output": "client result"}]
    restored = json.dumps(policy.restore_replay(continuation, "responses", "snapshot-test")["input"], ensure_ascii=False)
    assert restored.count("hidden result") == restored.count("opaque-test-value") == restored.count("我查一下") == 1


async def test_terminal_only_refusal_and_duplicate_external_calls():
    obj = reply("responses", mixed=True)
    obj["output"].append(copy.deepcopy(obj["output"][0]))
    obj["output"].append({"type": "message", "id": "refusal", "role": "assistant", "status": "completed",
                          "content": [{"type": "refusal", "refusal": "cannot comply"}]})
    async def invoke(_):
        async def source():
            yield encode("response.completed", {"type": "response.completed", "response": obj})
        return StreamingResponse(source())
    _, frames = await collect(policy.stream(request(mixed=True), "responses", invoke))
    assert frames[-1]["type"] == "response.completed"
    assert "".join(f["delta"] for f in frames if f["type"] == "response.refusal.delta") == "cannot comply"
    done = [f["item"] for f in frames if f["type"] == "response.output_item.done"]
    assert done == frames[-1]["response"]["output"]
    assert len([item for item in done if item["type"] == "function_call"]) == 1
