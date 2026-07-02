import asyncio

import pytest

from src import apikey_limiter


def _cfg(**limits):
    return {
        "apiKeyConcurrency": {
            "enabled": True,
            "defaultMaxConcurrent": 1,
            "defaultMaxQueue": 1,
            "defaultQueueWaitSeconds": 1,
        },
        "apiKeys": {
            "k": {
                "key": "secret",
                "enabled": True,
                "allowedModels": [],
                "allowImages": False,
                "limits": limits,
            }
        },
    }


@pytest.fixture(autouse=True)
def _reset_slots(monkeypatch):
    apikey_limiter._slots.clear()
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: _cfg())
    yield
    apikey_limiter._slots.clear()


@pytest.mark.asyncio
async def test_default_limit_queues_and_releases_fifo():
    first = await apikey_limiter.acquire("k")
    task = asyncio.create_task(apikey_limiter.acquire("k"))
    await asyncio.sleep(0.05)
    snap = apikey_limiter.key_snapshot("k")
    assert snap["in_flight"] == 1
    assert snap["waiting"] == 1

    await first.release()
    second = await asyncio.wait_for(task, timeout=1)
    assert second.queue_wait_ms >= 0
    await second.release()
    assert apikey_limiter.key_snapshot("k")["in_flight"] == 0


@pytest.mark.asyncio
async def test_queue_full_returns_429_error():
    first = await apikey_limiter.acquire("k")
    queued = asyncio.create_task(apikey_limiter.acquire("k"))
    await asyncio.sleep(0.05)
    with pytest.raises(apikey_limiter.ApiKeyLimitError) as ei:
        await apikey_limiter.acquire("k")
    assert ei.value.reason == "queue_full"
    queued.cancel()
    await first.release()


@pytest.mark.asyncio
async def test_key_limits_enabled_overrides_global(monkeypatch):
    monkeypatch.setattr(
        apikey_limiter.config,
        "get",
        lambda: {
            "apiKeyConcurrency": {
                "enabled": False,
                "defaultMaxConcurrent": 1,
                "defaultMaxQueue": 1,
                "defaultQueueWaitSeconds": 1,
            },
            "apiKeys": {"k": {"key": "secret", "limits": {"enabled": True, "maxConcurrent": 1}}},
        },
    )
    first = await apikey_limiter.acquire("k")
    queued = asyncio.create_task(apikey_limiter.acquire("k"))
    await asyncio.sleep(0.05)
    assert apikey_limiter.key_snapshot("k")["waiting"] == 1
    queued.cancel()
    await first.release()
