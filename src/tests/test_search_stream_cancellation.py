"""Deterministic queue-ready cancellation and heartbeat ownership regressions."""
import asyncio

import pytest

from src import search_tool_policy as policy, search_tool_stream as stream


@pytest.mark.parametrize("queue_ready", [False, True])
async def test_cancel_propagates_even_when_queue_get_just_completed(monkeypatch, queue_ready):
    entered, closed = asyncio.Event(), asyncio.Event()
    getters = []
    real_queue = asyncio.Queue

    class Queue(real_queue):
        async def get(self):
            getters.append(asyncio.current_task())
            entered.set()
            value = await super().get()
            # Cancel before the ready get's done callbacks can resume its
            # owner. Python 3.11 wait_for used to return value instead.
            asyncio.get_running_loop().call_soon(pull.cancel)
            return value

    async def run(*args, _stream, **kwargs):
        try:
            if queue_ready:
                await _stream.emit(b"ready text")
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(stream.asyncio, "Queue", Queue)
    monkeypatch.setattr(policy, "run", run)
    iterator = stream.stream({}, "chat", None).body_iterator
    pull = asyncio.create_task(anext(iterator))
    try:
        if not queue_ready:
            await asyncio.wait_for(entered.wait(), 1)
            pull.cancel()
        result = await asyncio.wait_for(asyncio.gather(pull, return_exceptions=True), 1)
        assert pull.cancelled(), result
        assert closed.is_set()
        assert getters and all(task.done() for task in getters)
    finally:
        if not pull.done():
            pull.cancel()
        await asyncio.gather(pull, return_exceptions=True)
        await iterator.aclose()


async def test_heartbeat_retires_timed_out_get_without_losing_next_text(monkeypatch):
    ready, closed = asyncio.Event(), asyncio.Event()
    getters = []
    real_queue, real_wait = asyncio.Queue, asyncio.wait
    waits = 0

    class Queue(real_queue):
        async def get(self):
            getters.append(asyncio.current_task())
            return await super().get()

    async def wait(tasks, *, timeout):
        nonlocal waits
        waits += 1
        assert timeout == 5  # Keep the production heartbeat interval unchanged.
        # Exercise an actual timeout without a five-second wall-clock sleep.
        return await real_wait(tasks, timeout=0 if waits == 1 else timeout)

    async def run(*args, _stream, **kwargs):
        try:
            await ready.wait()
            await _stream.emit(b"next text")
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(stream.asyncio, "Queue", Queue)
    monkeypatch.setattr(stream.asyncio, "wait", wait)
    monkeypatch.setattr(policy, "run", run)
    iterator = stream.stream({}, "chat", None).body_iterator
    try:
        assert await asyncio.wait_for(anext(iterator), 1) == b": parrot managed search\n\n"
        assert len(getters) == 1 and getters[0].cancelled()
        assert not closed.is_set()
        ready.set()
        assert await asyncio.wait_for(anext(iterator), 1) == b"next text"
    finally:
        await iterator.aclose()
    assert closed.is_set()
    assert all(task.done() for task in getters)


async def test_encoder_failure_does_not_turn_into_endless_heartbeats(monkeypatch):
    real_wait = asyncio.wait
    failed = asyncio.Event()

    async def wait(tasks, *, timeout):
        await failed.wait()
        return await real_wait(tasks, timeout=0)

    async def run(*args, **kwargs):
        raise RuntimeError("upstream failure")

    async def fail(self, payload):
        failed.set()
        raise ValueError("encoder failed")

    monkeypatch.setattr(stream.asyncio, "wait", wait)
    monkeypatch.setattr(policy, "run", run)
    monkeypatch.setattr(stream.Projection, "fail", fail)
    iterator = stream.stream({}, "chat", None).body_iterator
    with pytest.raises(ValueError, match="encoder failed"):
        await asyncio.wait_for(anext(iterator), 1)
    await iterator.aclose()
