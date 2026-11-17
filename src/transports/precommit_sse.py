"""Keep a translated Responses transport alive without committing an upstream.

Only a valid, buffered Responses start can open this transport. The ordinary
failover task still owns channel selection, IDs, billing and finalization. Until
it returns a response, downstream receives SSE comments, never attempt metadata.
"""
from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from collections.abc import Awaitable, Callable

from starlette.responses import Response, StreamingResponse

from .. import errors
from ..async_owned import await_owned

_READY: ContextVar[Callable[[], None] | None] = ContextVar("precommit_sse_ready", default=None)
KEEPALIVE_INTERVAL_SECONDS = 5.0
_KEEPALIVE = b": parrot keepalive\n\n"


def notify_buffered_response_start() -> None:
    callback = _READY.get()
    if callback is not None:
        callback()


class _ResponseOwner:
    def __init__(self, task: asyncio.Task[Response]):
        self.task = task
        self.inner_started = False
        self._close_task: asyncio.Task | None = None

    async def close(self) -> None:
        if self._close_task is None:
            async def cleanup():
                if not self.task.done():
                    self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
                if self.task.cancelled() or self.task.exception() is not None:
                    return
                response = self.task.result()
                if not self.inner_started:
                    abort = getattr(response, "_parrot_abort_unconsumed_stream", None)
                    if abort is not None:
                        await abort()
                iterator = getattr(response, "body_iterator", None)
                if iterator is not None and hasattr(iterator, "aclose"):
                    await iterator.aclose()
            self._close_task = asyncio.create_task(cleanup())
        await await_owned(self._close_task)


def _error_chunk(response: Response) -> bytes:
    try:
        payload = json.loads(bytes(getattr(response, "body", b"")).decode())
    except (ValueError, UnicodeDecodeError):
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else None
    error = error if isinstance(error, dict) else {}
    return errors.sse_error_line_responses(
        str(error.get("type") or errors.ErrTypeOpenAI.SERVER),
        str(error.get("message") or "upstream request failed"),
        code=error.get("code"),
        details=error.get("details") if isinstance(error.get("details"), dict) else None,
    )


class _KeepaliveIterator:
    def __init__(self, owner: _ResponseOwner):
        self.owner = owner
        self.iterator = self._iterate()
        self._close_task: asyncio.Task | None = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._close_task is not None:
            raise StopAsyncIteration
        try:
            return await self.iterator.__anext__()
        except BaseException:
            await self.aclose()
            raise

    async def _iterate(self):
        yield _KEEPALIVE
        while not self.owner.task.done():
            done, _ = await asyncio.wait(
                {self.owner.task}, timeout=KEEPALIVE_INTERVAL_SECONDS,
            )
            if not done:
                yield _KEEPALIVE
        response = self.owner.task.result()
        if isinstance(response, StreamingResponse):
            self.owner.inner_started = True
            async for chunk in response.body_iterator:
                yield chunk
            if response.background is not None:
                await response.background()
        else:
            # HTTP status can no longer change after the keepalive headers.
            # Retain the final error's code/type/message without inventing an ID.
            yield _error_chunk(response)

    async def aclose(self):
        if self._close_task is None:
            async def cleanup():
                try:
                    await self.iterator.aclose()
                finally:
                    await self.owner.close()
            self._close_task = asyncio.create_task(cleanup())
        await await_owned(self._close_task)


class _KeepaliveResponse(StreamingResponse):
    def __init__(self, owner: _ResponseOwner):
        self._owned_iterator = _KeepaliveIterator(owner)
        # An uncommitted attempt's request-id/model headers cannot identify a
        # later failover winner. Do not expose those as final response metadata.
        super().__init__(
            self._owned_iterator, media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._owned_iterator.aclose()


async def run_with_keepalive(operation: Callable[[], Awaitable[Response]]) -> Response:
    ready = asyncio.Event()

    async def run():
        token = _READY.set(ready.set)
        try:
            return await operation()
        finally:
            _READY.reset(token)

    task = asyncio.create_task(run())
    owner = _ResponseOwner(task)
    waiter = asyncio.create_task(ready.wait())
    handed_off = False
    try:
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            # Preserve ordinary status/headers when no long pre-commit wait is
            # necessary (including auth/guard errors and native fast streams).
            response = task.result()
        else:
            response = _KeepaliveResponse(owner)
        waiter.cancel()
        await await_owned(asyncio.gather(waiter, return_exceptions=True))
        # No await may follow the ownership transfer before the actual return.
        handed_off = True
        return response
    finally:
        waiter.cancel()
        if not handed_off:
            try:
                await await_owned(asyncio.gather(waiter, return_exceptions=True))
            finally:
                await owner.close()
