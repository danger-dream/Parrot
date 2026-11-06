"""Managed search rounds stay restorable after client-only turns, at bounded memory."""
import copy
import gc
import json
import tracemalloc

import pytest
from fastapi.responses import JSONResponse

from src import local_web_tools as web, search_service, search_tool_policy as policy
from src.tests.test_search_review_fixes import answer, req


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    policy._REPLAY.clear()
    settings = {**search_service.DEFAULTS, "maxToolRounds": 4}
    monkeypatch.setattr(web, "_settings", lambda: settings)
    monkeypatch.setattr(web, "max_tool_rounds", lambda: 4)

    async def execute(calls, **kw):
        return [web.LocalToolResult(c.id, "hidden " + c.id) for c in calls]

    monkeypatch.setattr(web, "execute_local_tool_calls", execute)
    yield
    policy._REPLAY.clear()


def _result(protocol, call_id, text):
    if protocol == "responses":
        return {"type": "function_call_output", "call_id": call_id, "output": text}
    if protocol == "chat":
        return {"role": "tool", "tool_call_id": call_id, "content": text}
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": text}]}


async def _turn(protocol, body, visible, calls, *, check=None, result_text=None):
    """One client turn: ingress expansion, run(), then the client's tool result."""
    async def invoke(current):
        if check:
            check(current)
            check_done.append(True)
        return JSONResponse(answer(protocol, calls.pop(0)))

    check_done = []
    expanded = policy.restore_replay(body, protocol, "k")
    response = json.loads((await policy.run(expanded, protocol, invoke, api_key_name="k")).body)
    assert not check or check_done
    policy._append(visible, response, [], protocol)
    client_id = [c for c in _client_ids(protocol, response)][0]
    policy._history(visible, protocol).append(_result(protocol, client_id, result_text or f"client result {client_id}"))
    return copy.deepcopy(visible)


def _client_ids(protocol, response):
    if protocol == "responses":
        return [o["call_id"] for o in response["output"] if o.get("type") == "function_call"]
    if protocol == "chat":
        return [c["id"] for c in response["choices"][0]["message"].get("tool_calls") or []]
    return [b["id"] for b in response["content"] if b.get("type") == "tool_use"]


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
async def test_hidden_round_is_restored_after_client_only_turns(protocol):
    body = req(protocol)
    visible = copy.deepcopy(body)
    body = await _turn(protocol, body, visible, [[("client1", "calculate", {})]])
    body = await _turn(protocol, body, visible, [[("client2", "calculate", {})]])
    # Turn 3: Parrot runs the search, the client only sees its own call.
    body = await _turn(protocol, body, visible, [[("search3", "web_search", {"query": "docs"}), ("client3", "calculate", {})]])
    assert "hidden search3" not in json.dumps(body)

    def sees_search_once(current):
        text = json.dumps(current)
        assert text.count("hidden search3") == 1 and text.count('"search3"') >= 1
        assert "client result client3" in text

    body = await _turn(protocol, body, visible, [[("client4", "calculate", {})]], check=sees_search_once)
    body = await _turn(protocol, body, visible, [[("client5", "calculate", {})]], check=sees_search_once)
    once = policy.restore_replay(body, protocol, "k")
    assert json.dumps(once).count("hidden search3") == 1
    assert policy.restore_replay(once, protocol, "k") == once


@pytest.mark.parametrize("protocol", ["chat", "anthropic"])
async def test_entries_without_hidden_content_are_not_cached(protocol):
    body = req(protocol)
    visible = copy.deepcopy(body)
    for turn in range(1, 4):
        body = await _turn(protocol, body, visible, [[(f"client{turn}", "calculate", {})]])
    assert not policy._REPLAY
    body = await _turn(protocol, body, visible, [[("search4", "web_search", {"query": "q"}), ("client4", "calculate", {})]])
    assert len(policy._REPLAY) == 1


def test_previous_response_id_delta_still_expands_entries_without_hidden_content():
    saved = req("responses")
    saved["input"].append({"type": "function_call", "call_id": "c1", "name": "calculate", "arguments": "{}"})
    policy._remember(copy.deepcopy(saved), "responses", "k", ["c1", "resp_1"], visible_body=copy.deepcopy(saved))
    delta = {"model": "m", "previous_response_id": "resp_1", "input": [_result("responses", "c1", "ok")]}
    out = policy.restore_replay(delta, "responses", "k")
    assert out["input"] == saved["input"] + delta["input"] and "previous_response_id" not in out
    full = {"model": "m", "tools": saved["tools"], "input": saved["input"] + delta["input"]}
    assert policy.restore_replay(full, "responses", "k") is full


@pytest.mark.parametrize("protocol", ["chat", "anthropic", "responses"])
async def test_restored_conversation_keeps_one_copy_of_its_history(protocol):
    body = req(protocol)
    visible = copy.deepcopy(body)
    body = await _turn(protocol, body, visible, [[("search1", "web_search", {"query": "q"}), ("client1", "calculate", {})]])
    gc.collect()
    tracemalloc.start()
    for turn in range(2, 32):
        body = await _turn(protocol, body, visible, [[(f"client{turn}", "calculate", {})]],
                           result_text=f"{turn} " + "中" * 20000)
        body = json.loads(json.dumps(body))  # every request arrives freshly parsed
    del body
    gc.collect()
    retained, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    one_copy = 30 * 20000 * 2
    # The test keeps `visible` (one copy); the cache holds about one more.
    assert retained < 3.5 * one_copy, (retained, one_copy)
    assert len(policy._REPLAY) >= 30
