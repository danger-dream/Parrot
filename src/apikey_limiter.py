"""下游 API Key 级并发限制 + FIFO 排队。

这层和 ``src.concurrency`` 的职责不同：
- apiKeyLimiter 保护下游租户 / API Key 公平性；
- concurrency 保护上游渠道并发。

配置：
  apiKeyConcurrency.enabled/defaultMaxConcurrent/defaultMaxQueue/defaultQueueWaitSeconds
  apiKeys.<name>.limits.enabled/maxConcurrent/maxQueue/queueWaitSeconds

单 Key 的 limits.enabled 优先级高于全局 enabled；其它 limits 字段缺失 / null 时继承全局默认。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import Request
from starlette.responses import Response

from . import config


@dataclass
class Waiter:
    future: asyncio.Future
    enqueued_at: float


@dataclass
class ResolvedLimits:
    enabled: bool
    max_concurrent: int
    max_queue: int
    queue_wait_seconds: int
    enabled_source: str = "global"
    max_concurrent_source: str = "global"
    max_queue_source: str = "global"
    queue_wait_source: str = "global"

    @property
    def unlimited(self) -> bool:
        return self.max_concurrent <= 0


@dataclass
class ApiKeySlot:
    key_name: str
    limits: ResolvedLimits
    in_flight: int = 0
    waiters: list[Waiter] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ApiKeyLimitError(Exception):
    """API Key limiter 拒绝请求（队列满 / 等待超时 / 客户端断开）。"""

    def __init__(self, message: str, *, reason: str, retry_after: int | None = None,
                 headers: Optional[dict[str, str]] = None):
        super().__init__(message)
        self.message = message
        self.reason = reason
        self.retry_after = retry_after
        self.headers = headers or {}


class ApiKeyLease:
    """一次 API Key slot 占用。release 幂等。"""

    def __init__(self, key_name: str, slot: ApiKeySlot | None,
                 limits: ResolvedLimits | None, *, queue_wait_ms: int = 0,
                 noop: bool = False):
        self.key_name = key_name
        self.slot = slot
        self.limits = limits
        self.queue_wait_ms = int(queue_wait_ms)
        self.noop = noop
        self._released = False
        self.attached_to_response = False

    async def release(self) -> None:
        if self.noop or self._released or self.slot is None:
            self._released = True
            return
        self._released = True
        await _release_slot(self.slot)


_slots: dict[str, ApiKeySlot] = {}
_slots_lock = asyncio.Lock()

_DEFAULT_MAX_CONCURRENT = 5
_DEFAULT_MAX_QUEUE = 50
_DEFAULT_QUEUE_WAIT_SECONDS = 1800


def _as_int(value: Any, default: int, *, minimum: int = 0) -> int:
    try:
        iv = int(value)
    except Exception:
        return default
    if iv < minimum:
        return default
    return iv


def _resolve_limits(key_name: str) -> ResolvedLimits:
    cfg = config.get()
    global_cfg = cfg.get("apiKeyConcurrency") or {}
    global_enabled = bool(global_cfg.get("enabled", True))
    global_max = _as_int(global_cfg.get("defaultMaxConcurrent"), _DEFAULT_MAX_CONCURRENT)
    global_queue = _as_int(global_cfg.get("defaultMaxQueue"), _DEFAULT_MAX_QUEUE)
    global_wait = _as_int(global_cfg.get("defaultQueueWaitSeconds"), _DEFAULT_QUEUE_WAIT_SECONDS)

    entry = (cfg.get("apiKeys") or {}).get(key_name)
    limits = entry.get("limits") if isinstance(entry, dict) else None
    if not isinstance(limits, dict):
        limits = {}

    if "enabled" in limits and limits.get("enabled") is not None:
        enabled = bool(limits.get("enabled"))
        enabled_source = "key"
    else:
        enabled = global_enabled
        enabled_source = "global"

    if limits.get("maxConcurrent") is not None:
        max_concurrent = _as_int(limits.get("maxConcurrent"), global_max)
        max_source = "key"
    else:
        max_concurrent = global_max
        max_source = "global"

    if limits.get("maxQueue") is not None:
        max_queue = _as_int(limits.get("maxQueue"), global_queue)
        queue_source = "key"
    else:
        max_queue = global_queue
        queue_source = "global"

    if limits.get("queueWaitSeconds") is not None:
        queue_wait = _as_int(limits.get("queueWaitSeconds"), global_wait)
        wait_source = "key"
    else:
        queue_wait = global_wait
        wait_source = "global"

    return ResolvedLimits(
        enabled=enabled,
        max_concurrent=max_concurrent,
        max_queue=max_queue,
        queue_wait_seconds=queue_wait,
        enabled_source=enabled_source,
        max_concurrent_source=max_source,
        max_queue_source=queue_source,
        queue_wait_source=wait_source,
    )


async def _get_slot(key_name: str) -> ApiKeySlot:
    async with _slots_lock:
        limits = _resolve_limits(key_name)
        slot = _slots.get(key_name)
        if slot is None:
            slot = ApiKeySlot(key_name=key_name, limits=limits)
            _slots[key_name] = slot
        else:
            slot.limits = limits
        return slot


async def acquire(key_name: str | None, request: Request | None = None) -> ApiKeyLease:
    """占用一个 API Key 并发槽位；必要时进入 FIFO 队列。

    返回 ApiKeyLease，调用方必须在请求生命周期结束时 release。
    """
    key = str(key_name or "").strip()
    if not key:
        return ApiKeyLease("", None, None, noop=True)

    slot = await _get_slot(key)
    limits = slot.limits
    if not limits.enabled:
        return ApiKeyLease(key, None, limits, noop=True)

    start_wait = time.monotonic()

    # 快速路径：不限并发或还有空位。
    async with slot.lock:
        slot.limits = _resolve_limits(key)
        limits = slot.limits
        if not limits.enabled:
            return ApiKeyLease(key, None, limits, noop=True)
        if limits.unlimited or slot.in_flight < limits.max_concurrent:
            slot.in_flight += 1
            return ApiKeyLease(key, slot, limits, queue_wait_ms=0)

        if limits.max_queue <= 0 or len(slot.waiters) >= limits.max_queue:
            raise _queue_full_error(key, slot)

        fut = asyncio.get_event_loop().create_future()
        waiter = Waiter(future=fut, enqueued_at=time.monotonic())
        slot.waiters.append(waiter)

    try:
        await _wait_for_turn_or_abort(slot, waiter, request, limits.queue_wait_seconds)
        queue_wait_ms = int((time.monotonic() - start_wait) * 1000)
        # 被 release 唤醒后真正占位；若热加载导致仍无空位，则重新排队。
        while True:
            async with slot.lock:
                slot.limits = _resolve_limits(key)
                limits = slot.limits
                if not limits.enabled:
                    return ApiKeyLease(key, None, limits, noop=True)
                if limits.unlimited or slot.in_flight < limits.max_concurrent:
                    slot.in_flight += 1
                    return ApiKeyLease(key, slot, limits, queue_wait_ms=queue_wait_ms)
                if limits.max_queue <= 0 or len(slot.waiters) >= limits.max_queue:
                    raise _queue_full_error(key, slot)
                fut = asyncio.get_event_loop().create_future()
                waiter = Waiter(future=fut, enqueued_at=time.monotonic())
                slot.waiters.append(waiter)
            await _wait_for_turn_or_abort(slot, waiter, request, limits.queue_wait_seconds)
    except BaseException:
        await _drop_waiter(slot, waiter)
        raise


async def _wait_for_turn_or_abort(
    slot: ApiKeySlot,
    waiter: Waiter,
    request: Request | None,
    timeout_seconds: int,
) -> None:
    wait_items: list[asyncio.Future | asyncio.Task] = [waiter.future]
    timeout_task: asyncio.Task | None = None
    disconnect_task: asyncio.Task | None = None
    if timeout_seconds <= 0:
        await _drop_waiter(slot, waiter)
        raise _timeout_error(slot.key_name, slot, timeout_seconds)
    timeout_task = asyncio.create_task(asyncio.sleep(timeout_seconds))
    wait_items.append(timeout_task)
    if request is not None:
        disconnect_task = asyncio.create_task(_wait_http_disconnect(request))
        wait_items.append(disconnect_task)

    try:
        done, _pending = await asyncio.wait(wait_items, return_when=asyncio.FIRST_COMPLETED)
        if waiter.future in done:
            return
        await _drop_waiter(slot, waiter)
        if disconnect_task is not None and disconnect_task in done:
            raise ApiKeyLimitError(
                f"API key '{slot.key_name}' queued request was cancelled because the client disconnected.",
                reason="client_disconnected",
            )
        raise _timeout_error(slot.key_name, slot, timeout_seconds)
    finally:
        for item in wait_items:
            if item is not waiter.future and not item.done():
                item.cancel()


async def _wait_http_disconnect(request: Request) -> None:
    while True:
        try:
            if await request.is_disconnected():
                return
        except Exception:
            return
        await asyncio.sleep(1.0)


async def _release_slot(slot: ApiKeySlot) -> None:
    async with slot.lock:
        if slot.in_flight > 0:
            slot.in_flight -= 1
        while slot.waiters:
            waiter = slot.waiters.pop(0)
            fut = waiter.future
            if not fut.done():
                fut.set_result(None)
                break


async def _drop_waiter(slot: ApiKeySlot, waiter: Waiter) -> None:
    async with slot.lock:
        try:
            slot.waiters.remove(waiter)
        except ValueError:
            pass
    if not waiter.future.done():
        waiter.future.cancel()


def _queue_full_error(key_name: str, slot: ApiKeySlot) -> ApiKeyLimitError:
    limits = slot.limits
    msg = (
        f"API key '{key_name}' concurrency limit reached and queue is full "
        f"({len(slot.waiters)}/{limits.max_queue})."
    )
    return ApiKeyLimitError(msg, reason="queue_full", retry_after=30, headers=_headers(slot, 0))


def _timeout_error(key_name: str, slot: ApiKeySlot, wait_s: int) -> ApiKeyLimitError:
    msg = f"API key '{key_name}' queue wait timed out after {wait_s}s."
    return ApiKeyLimitError(msg, reason="queue_timeout", retry_after=30, headers=_headers(slot, wait_s * 1000))


def _headers(slot: ApiKeySlot | None, queue_wait_ms: int = 0) -> dict[str, str]:
    if slot is None:
        return {}
    limits = slot.limits
    headers = {
        "X-Parrot-ApiKey-In-Flight": str(slot.in_flight),
        "X-Parrot-ApiKey-Queued": str(len(slot.waiters)),
        "X-Parrot-ApiKey-Max-Concurrent": "unlimited" if limits.unlimited else str(limits.max_concurrent),
        "X-Parrot-ApiKey-Max-Queue": str(limits.max_queue),
        "X-Parrot-Queue-Wait-Ms": str(max(0, int(queue_wait_ms))),
    }
    return headers


def attach_release_to_response(response: Response, lease: ApiKeyLease) -> Response:
    """把 lease 释放绑定到响应体发送结束；StreamingResponse 支持客户端断开释放。"""
    if lease.noop or lease._released:
        return response
    lease.attached_to_response = True
    for k, v in _headers(lease.slot, lease.queue_wait_ms).items():
        try:
            response.headers[k] = v
        except Exception:
            pass

    body_iterator = getattr(response, "body_iterator", None)
    if body_iterator is None:
        # 兜底没有 iterator 的情况。
        async def _release_later():
            await lease.release()
        try:
            asyncio.create_task(_release_later())
        except RuntimeError:
            pass
        return response

    async def _wrapped_body_iterator():
        try:
            async for chunk in body_iterator:
                yield chunk
        finally:
            await lease.release()

    response.body_iterator = _wrapped_body_iterator()
    return response


def _oldest_wait_seconds(slot: ApiKeySlot) -> int:
    if not slot.waiters:
        return 0
    oldest = min(w.enqueued_at for w in slot.waiters)
    return max(0, int(time.monotonic() - oldest))


def snapshot() -> list[dict[str, Any]]:
    """供 TG Bot 展示：每个已追踪 API Key 的实时状态。"""
    out: list[dict[str, Any]] = []
    for key, slot in _slots.items():
        slot.limits = _resolve_limits(key)
        limits = slot.limits
        out.append({
            "key_name": key,
            "enabled": limits.enabled,
            "in_flight": slot.in_flight,
            "max_concurrent": limits.max_concurrent,
            "max_queue": limits.max_queue,
            "queue_wait_seconds": limits.queue_wait_seconds,
            "waiting": len(slot.waiters),
            "oldest_wait_seconds": _oldest_wait_seconds(slot),
            "unlimited": limits.unlimited,
            "enabled_source": limits.enabled_source,
            "max_concurrent_source": limits.max_concurrent_source,
            "max_queue_source": limits.max_queue_source,
            "queue_wait_source": limits.queue_wait_source,
        })
    out.sort(key=lambda x: x["key_name"])
    return out


def key_snapshot(key_name: str) -> dict[str, Any]:
    key = str(key_name or "")
    slot = _slots.get(key)
    limits = _resolve_limits(key)
    if slot is None:
        return {
            "key_name": key,
            "enabled": limits.enabled,
            "in_flight": 0,
            "max_concurrent": limits.max_concurrent,
            "max_queue": limits.max_queue,
            "queue_wait_seconds": limits.queue_wait_seconds,
            "waiting": 0,
            "oldest_wait_seconds": 0,
            "unlimited": limits.unlimited,
            "enabled_source": limits.enabled_source,
            "max_concurrent_source": limits.max_concurrent_source,
            "max_queue_source": limits.max_queue_source,
            "queue_wait_source": limits.queue_wait_source,
        }
    slot.limits = limits
    return {
        "key_name": key,
        "enabled": limits.enabled,
        "in_flight": slot.in_flight,
        "max_concurrent": limits.max_concurrent,
        "max_queue": limits.max_queue,
        "queue_wait_seconds": limits.queue_wait_seconds,
        "waiting": len(slot.waiters),
        "oldest_wait_seconds": _oldest_wait_seconds(slot),
        "unlimited": limits.unlimited,
        "enabled_source": limits.enabled_source,
        "max_concurrent_source": limits.max_concurrent_source,
        "max_queue_source": limits.max_queue_source,
        "queue_wait_source": limits.queue_wait_source,
    }


def totals() -> dict[str, int]:
    in_flight = sum(s.in_flight for s in _slots.values())
    waiting = sum(len(s.waiters) for s in _slots.values())
    return {"in_flight": in_flight, "waiting": waiting, "tracked_keys": len(_slots)}


def forget_key(key_name: str) -> None:
    slot = _slots.get(key_name)
    if slot is None:
        return
    if slot.in_flight > 0 or slot.waiters:
        return
    _slots.pop(key_name, None)
