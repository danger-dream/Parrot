import copy
import json
import tracemalloc
from collections import OrderedDict

import pytest

from src import search_tool_policy as policy

TOOLS = [{"type": "function", "function": {"name": "web_search"}}, {"type": "function", "function": {"name": "bash"}}]


@pytest.fixture(autouse=True)
def fresh_replay(monkeypatch):
    monkeypatch.setattr(policy, "_REPLAY", OrderedDict())


def _call(protocol, cid, name="bash"):
    if protocol == "chat":
        return {"role": "assistant", "content": None,
                "tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}]}
    if protocol == "anthropic":
        return {"role": "assistant", "content": [{"type": "tool_use", "id": cid, "name": name, "input": {}}]}
    return {"type": "function_call", "call_id": cid, "name": name, "arguments": "{}"}


def _result(protocol, cid, text):
    if protocol == "chat":
        return {"role": "tool", "tool_call_id": cid, "content": text}
    if protocol == "anthropic":
        return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": cid, "content": text}]}
    return {"type": "function_call_output", "call_id": cid, "output": text}


def _user(protocol, text, **extra):
    if protocol == "responses":
        return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}], **extra}
    return {"role": "user", "content": text, **extra}


def _conversation(protocol, name, turns, hidden_turns, *, variant_turn=None, delta_turns=()):
    """Yield (request_json, final_history, visible_history, refs) like run() would see."""
    field = "input" if protocol == "responses" else "messages"
    visible = [_user(protocol, f"{name} start " + "x" * 300, flag=True, n=1, z=0.0, order={"a": 1, "b": 2})]
    previous_id = None
    for turn in range(1, turns + 1):
        sent = copy.deepcopy(visible)
        if variant_turn == turn:
            first = sent[0]
            first["flag"], first["n"], first["z"] = 1, 1.0, -0.0
            first["order"] = {"b": 2, "a": 1}
        body = {"model": "m", "tools": TOOLS, field: sent}
        if protocol == "responses" and turn in delta_turns and previous_id:
            body = {"model": "m", "previous_response_id": previous_id, field: sent[-1:]}
        cid, sid = f"{name}-c{turn}", f"{name}-s{turn}"
        hidden = [_call(protocol, sid, "web_search"), _result(protocol, sid, f"results {turn}")] if turn in hidden_turns else []
        assistant = _call(protocol, cid)
        refs = [cid] + ([f"{name}-resp{turn}"] if protocol == "responses" else [])
        yield json.dumps(body), hidden, assistant, refs
        visible = sent + [assistant, _result(protocol, cid, f"{name} out {turn} " + "y" * 200)]
        previous_id = f"{name}-resp{turn}"


def _run(protocol, share_enabled, monkeypatch):
    if not share_enabled:
        monkeypatch.setattr(policy, "_share", lambda items, previous: None)
    field = "input" if protocol == "responses" else "messages"
    a = list(_conversation(protocol, "A", 8, {3, 6}, variant_turn=5, delta_turns={4, 7}))
    b = list(_conversation(protocol, "B", 6, {2}))
    # Hidden rounds from the first turn: no earlier entry shadows them, so later
    # turns restore (emit) cached history.
    c = list(_conversation(protocol, "C", 6, {1, 2, 4}, variant_turn=3))
    schedule = [a[0], b[0], c[0], a[1], c[1], a[2], b[1], c[2], b[2], a[3], c[3], a[4], b[3],
                a[5], c[4], a[6], b[4], a[7], c[5], b[5]]
    branch = json.loads(a[6][0])
    branch[field] = (branch.get(field) or [])[:3] + [_user(protocol, "branch")]
    schedule.insert(9, (json.dumps(branch), [], _call(protocol, "A-branch"), ["A-branch"]))
    trace = []
    for request_json, hidden, assistant, refs in schedule:
        request = json.loads(request_json)
        restored = policy.restore_replay(request, protocol, "key")
        visible_base = policy._visible_body(restored, protocol, "key")
        trace.append(json.dumps(restored))
        trace.append(json.dumps(visible_base))
        final = copy.deepcopy(restored)
        final[field] = list(policy._history(final, protocol)) + hidden + [assistant]
        visible = copy.deepcopy(visible_base)
        visible[field] = list(policy._history(visible, protocol)) + [assistant]
        policy._remember(final, protocol, "key", refs, visible_body=visible)
        trace.append(repr(list(policy._REPLAY)))
    return trace


@pytest.mark.parametrize("protocol", ["chat", "anthropic", "responses"])
def test_sharing_never_changes_replay_results(protocol, monkeypatch):
    shared = _run(protocol, True, monkeypatch)
    policy._REPLAY.clear()
    with monkeypatch.context() as m:
        unshared = _run(protocol, False, m)
    assert shared == unshared


def test_redundant_entry_still_shadows_hidden_round_like_before():
    protocol = "chat"
    t1 = {"model": "m", "tools": TOOLS, "messages": [_user(protocol, "start")]}
    a1 = _call(protocol, "c1")
    saved1 = copy.deepcopy(t1)
    saved1["messages"].append(a1)
    policy._remember(saved1, protocol, "k", ["c1"], visible_body=copy.deepcopy(saved1))
    shown = saved1["messages"] + [_result(protocol, "c1", "ok1")]
    hidden = [_call(protocol, "s1", "web_search"), _result(protocol, "s1", "results")]
    a2 = _call(protocol, "c2")
    policy._remember({"model": "m", "messages": shown + hidden + [a2]}, protocol, "k", ["c2"],
                     visible_body={"model": "m", "messages": shown + [a2]})
    t3 = {"model": "m", "tools": TOOLS, "messages": copy.deepcopy(shown) + [a2, _result(protocol, "c2", "ok2")]}
    assert policy.restore_replay(t3, protocol, "k") is t3


def test_consecutive_turns_share_one_copy_of_the_history():
    history = [_user("chat", f"message {i} " + "中" * 20000) for i in range(30)]
    tracemalloc.start()
    for turn in range(1, 31):
        request = json.loads(json.dumps({"model": "m", "messages": history[:turn]}))
        final = copy.deepcopy(request)
        final["messages"].append(_call("chat", f"c{turn}"))
        policy._remember(final, "chat", "k", [f"c{turn}"], visible_body=copy.deepcopy(final))
        del request, final
    current, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    one_copy = sum(len(m["content"]) * 2 for m in history)
    assert current < 3 * one_copy
    entries = list(policy._REPLAY.values())
    assert entries[-1][1]["messages"][0] is entries[0][1]["messages"][0]
    assert entries[-1][2][5] is entries[-1][1]["messages"][5]


@pytest.mark.parametrize("a,b", [
    (True, 1), (1, 1.0), (0.0, -0.0), ({"a": 1, "b": 2}, {"b": 2, "a": 1}),
    ([1, 2], (1, 2)), ({"x": [True]}, {"x": [1]}), (float("nan"), float("nan")), (object(), object()),
])
def test_values_that_are_only_equal_are_not_interchangeable(a, b):
    assert not policy._same(a, b)


def test_indistinguishable_values_are_interchangeable():
    value = {"role": "user", "content": [{"type": "text", "text": "中文 🦜", "n": 1, "f": 1.5, "ok": False, "none": None}]}
    assert policy._same(value, json.loads(json.dumps(value)))
