"""A client disconnect must terminalize a handed-off attempt even when the
cancellation is delivered repeatedly (ASGI level cancellation fanned out by a
wrapper's gather), instead of leaving request_log pending for the stale cleaner.
"""
import asyncio
import copy
import os
import time

import httpx
import pytest
from starlette.responses import StreamingResponse

from src import config, failover
from src import search_tool_policy as policy, search_tool_stream as stream
from src.channel import registry
from src.transports.http_runtime import OpenedHttpResponse


@pytest.fixture(autouse=True)
def clean_config(monkeypatch):
    assert os.environ['PARROT_TEST_NETWORK_GUARD'] == 'loopback-only'
    monkeypatch.setattr(config, '_reload_callbacks', [])
    config.update(lambda c: (c.clear(), c.update(copy.deepcopy(config.DEFAULT_CONFIG))))
    monkeypatch.setattr(registry, '_sync_state_db_with_channels', lambda: None)
    registry.rebuild_from_config()


async def _several_loop_turns():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_disconnected_managed_search_stream_cancels_producer_once(monkeypatch):
    waiting, disconnected = asyncio.Event(), asyncio.Event()
    settled = []

    async def run(*args, _stream, **kwargs):
        try:
            waiting.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Same shape as a handed-off attempt finalizer awaiting I/O.
            await asyncio.shield(_several_loop_turns())
            await asyncio.shield(_several_loop_turns())
            settled.append(True)
            raise

    monkeypatch.setattr(policy, "run", run)
    response = stream.stream({}, "chat", None)
    assert isinstance(response, StreamingResponse)

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(_message):
        return None

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}}
    task = asyncio.create_task(response(scope, receive, send))
    await asyncio.wait_for(waiting.wait(), 1)
    disconnected.set()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)

    assert settled == [True]


@pytest.mark.asyncio
async def test_precommit_cancel_finalizes_attempt_under_repeated_cancellation(monkeypatch):
    config.update(lambda c: c.update(channels=[{
        'name': 'A', 'protocol': 'anthropic', 'enabled': True,
        'baseUrl': 'https://fixture.invalid', 'apiKey': 'review-fixture',
        'models': [{'real': 'demo', 'alias': 'demo'}],
    }]))
    registry.rebuild_from_config()
    ch = registry.get_channel('api:A')

    consuming = asyncio.Event()
    retry_updates, finished = [], []

    async def open_response(**_kwargs):
        return OpenedHttpResponse(
            ctx=object(),
            response=httpx.Response(200, headers={"content-type": "text/event-stream"}),
            connect_ms=1,
        )

    async def consume_stream(*_args, **_kwargs):
        consuming.set()
        await asyncio.Event().wait()

    async def finalize_opened(*_args, **_kwargs):
        await _several_loop_turns()

    async def safe_exit(_ctx):
        await _several_loop_turns()

    monkeypatch.setattr(failover, "open_response_with_proxy_chain", open_response)
    monkeypatch.setattr(failover, "_consume_stream", consume_stream)
    monkeypatch.setattr(failover, "finalize_opened_http_response", finalize_opened)
    monkeypatch.setattr(failover, "_safe_exit", safe_exit)
    monkeypatch.setattr(failover.log_db, "update_pending_fast_mode_from_upstream", lambda *a, **k: None)
    monkeypatch.setattr(failover.log_db, "update_retry_attempt",
                        lambda attempt_id, **kw: retry_updates.append((attempt_id, kw["outcome"])))
    monkeypatch.setattr(failover.log_db, "finish_error",
                        lambda request_id, message, *a, **kw: finished.append((request_id, message, kw["status"])))

    now = time.time()
    body = {'model': 'demo', 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 16, 'stream': True}
    task = asyncio.create_task(failover._try_channel(
        ch, 'demo', body, True, now + 60, now, None, [], None, '127.0.0.1', 'req-1', 0, 0,
        ingress_protocol='anthropic', retry_attempt_id=7,
        start_monotonic=time.monotonic(), attempt_start_monotonic=time.monotonic(),
    ))
    await asyncio.wait_for(consuming.wait(), 1)
    # A wrapper whose gather is cancelled every loop turn re-cancels its child.
    for _ in range(200):
        if task.done():
            break
        task.cancel()
        await asyncio.sleep(0)
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)

    assert task.cancelled()
    assert retry_updates == [(7, "cancelled")]
    assert finished == [("req-1", "client disconnected", "cancelled")]
