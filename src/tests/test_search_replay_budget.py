import asyncio
from collections import OrderedDict

import pytest

from src import search_tool_policy as policy


@pytest.fixture(autouse=True)
def fresh_replay(monkeypatch):
    monkeypatch.setattr(policy, "_REPLAY", OrderedDict())


def _remember(reference, text):
    body = {"model": "m", "input": [{"type": "message", "role": "user", "content": text}]}
    policy._remember(body, "responses", "key", [reference])


def _age_all_entries():
    for key, (when, saved, visible) in list(policy._REPLAY.items()):
        policy._REPLAY[key] = (when - policy._REPLAY_TTL - 1, saved, visible)


def test_entry_limit_still_evicts_oldest(monkeypatch):
    monkeypatch.setattr(policy, "_REPLAY_LIMIT", 3)
    for index in range(5):
        _remember(f"resp_{index}", f"text {index}")
    assert [key[3] for key in policy._REPLAY] == ["resp_2", "resp_3", "resp_4"]


def test_large_histories_are_not_evicted_by_size():
    for index in range(6):
        _remember(f"resp_{index}", "x" * 2_000_000 + str(index))
    assert len(policy._REPLAY) == 6


def test_expired_entries_leave_on_read():
    _remember("resp_old", "old")
    assert policy._REPLAY
    _age_all_entries()
    policy.restore_replay({"model": "m", "input": "next", "previous_response_id": "resp_old"}, "responses", "key")
    assert not policy._REPLAY


def test_cleanup_loop_prunes_without_traffic(monkeypatch):
    _remember("resp_old", "old")
    monkeypatch.setattr(policy, "_REPLAY_CLEANUP_INTERVAL", 0.01)
    _age_all_entries()

    async def scenario():
        task = asyncio.create_task(policy.replay_cleanup_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert not policy._REPLAY


def test_replay_identity_digest_is_unchanged():
    import hashlib
    import json
    value = [[{"role": "user", "content": "中文 🦜", "b": 1, "a": [1.5, None]}], [{"z": 1, "y": {"k": "v" * 70000}}]]
    expected = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert policy._digest(value) == expected
