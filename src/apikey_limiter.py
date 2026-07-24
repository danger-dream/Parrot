"""下游 API Key 级并发限制 + FIFO 排队。

这层和 ``src.concurrency`` 的职责不同：
- apiKeyLimiter 保护下游租户 / API Key 公平性；
- concurrency 保护上游渠道并发。

配置：
  apiKeyConcurrency.enabled/defaultMaxConcurrent/defaultMaxQueue/defaultQueueWaitSeconds
  apiKeyConcurrency.defaultMaxRequestBodyBytes/defaultMaxRequestBodyEvents
  apiKeyConcurrency.defaultMaxQueuedBodyBytesPerKey/maxQueuedBodyBytes
  apiKeyConcurrency.queuedBodySpoolThresholdBytes
  apiKeyConcurrency.defaultMaxQueuedBodySpoolBytesPerKey/maxQueuedBodySpoolBytes
  (legacy maxQueuedBodyBytesTotal is accepted only as a fallback)
  apiKeys.<name>.limits may override the corresponding per-key/body defaults with
  enabled/maxConcurrent/maxQueue/queueWaitSeconds and
  maxRequestBodyBytes/maxRequestBodyEvents/maxQueuedBodyBytes/
  maxQueuedBodySpoolBytes.

单 Key 的 limits.enabled 优先级高于全局 enabled；其它 limits 字段缺失 / null 时继承全局默认。
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import weakref
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from fastapi import Request
from starlette.responses import Response
from starlette.types import Message, Receive

from . import config


@dataclass
class Waiter:
    future: asyncio.Future
    enqueued_at: float
    capacity_reserved: bool = False


@dataclass
class ResolvedLimits:
    enabled: bool
    max_concurrent: int
    max_queue: int
    queue_wait_seconds: int
    max_request_body_bytes: int
    max_request_body_events: int
    max_queued_body_bytes: int
    max_queued_body_bytes_total: int
    queued_body_spool_threshold_bytes: int
    max_queued_body_spool_bytes: int
    max_queued_body_spool_bytes_total: int
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


DEFAULT_MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_REQUEST_BODY_EVENTS = 1024
DEFAULT_MAX_QUEUED_BODY_BYTES_PER_KEY = 32 * 1024 * 1024
DEFAULT_MAX_QUEUED_BODY_BYTES_TOTAL = 128 * 1024 * 1024
DEFAULT_QUEUED_BODY_SPOOL_THRESHOLD_BYTES = 1024 * 1024
DEFAULT_MAX_QUEUED_BODY_SPOOL_BYTES_PER_KEY = 512 * 1024 * 1024
DEFAULT_MAX_QUEUED_BODY_SPOOL_BYTES_TOTAL = 2 * 1024 * 1024 * 1024
# Aggregate budgets include a conservative estimate for each retained ASGI dict.
QUEUED_BODY_EVENT_OVERHEAD_BYTES = 1024

# OpenAI's image edit contract allows up to 16 input images plus one mask.  A
# legal 20 MiB image is larger on the wire when carried as a JSON data URL, so
# the generic API-body default cannot be used as the total-body cap for these
# endpoints.  Multipart file parts are streamed by Starlette into spooled files
# after admission; only queued events are retained here, under the aggregate
# budgets below.
_COMPAT_IMAGE_EDIT_PATHS = frozenset({"/v1/images/edits", "/images/edits"})
_SINGLE_IMAGE_EDIT_PATHS = frozenset({"/v1/images/edit"})
_MEDIA_PROTOCOL_BODY_PATHS = frozenset({
    "/v1/images/generate",
    "/v1/images/edit",
    "/v1/images/generations",
    "/images/generations",
    "/v1/images/edits",
    "/images/edits",
    "/v1/videos",
    "/v1/videos/generations",
    "/v1/videos/edits",
    "/v1/videos/extensions",
})
_OPENAI_MAX_EDIT_IMAGES = 16
_IMAGE_BODY_ENVELOPE_BYTES = 1024 * 1024

_queued_body_lock = asyncio.Lock()
_queued_body_bytes_by_key: dict[str, int] = {}
_queued_body_bytes_total = 0
_queued_body_spool_bytes_by_key: dict[str, int] = {}
_queued_body_spool_bytes_total = 0
_active_body_guards: weakref.WeakSet[_BodyPreservingReceive] = weakref.WeakSet()


class RequestBodyTooLarge(Exception):
    """A request exceeded its total-body or queued aggregate budget."""

    def __init__(
        self,
        *,
        max_bytes: int,
        reason: str = "request_bytes",
        max_events: int | None = None,
    ):
        self.max_bytes = int(max_bytes)
        self.max_events = None if max_events is None else int(max_events)
        self.reason = reason
        if reason == "event_count":
            message = f"request has more than {self.max_events} body events"
        elif reason == "key_aggregate":
            message = f"API key queued-body budget exceeds {self.max_bytes} accounted bytes"
        elif reason == "process_aggregate":
            message = f"process queued-body budget exceeds {self.max_bytes} accounted bytes"
        else:
            message = f"request body exceeds {self.max_bytes} bytes"
        super().__init__(message)


class QueuedBodySpoolError(Exception):
    """A queued request body could not be safely spooled.

    The exception text is deliberately stable and contains no OS error detail.
    Callers may return it to clients without exposing configured paths.
    """

    def __init__(self, *, operation: str = "io"):
        self.operation = str(operation or "io")
        super().__init__("queued request body spool is temporarily unavailable")


def _spool_failure(operation: str, exc: BaseException) -> QueuedBodySpoolError:
    """Log bounded, non-secret diagnostics and return a stable client error."""
    errno = getattr(exc, "errno", None)
    errno_text = f" errno={errno}" if isinstance(errno, int) else ""
    print(
        f"[apikey_limiter] queued body spool {operation} failed "
        f"type={type(exc).__name__}{errno_text}"
    )
    return QueuedBodySpoolError(operation=operation)


@dataclass
class _BufferedBodyEvent:
    message_without_body: Message
    had_body: bool
    offset: int
    body_bytes: int
    memory_charge: int
    spool_charge: int


def _request_body_max_bytes(request: Request, limits: ResolvedLimits) -> int:
    """Resolve the total wire-body contract for this endpoint.

    ``images.maxInputImageBytes`` limits decoded images.  JSON/data-URL bodies
    need base64 expansion headroom, while multipart carries raw file bytes.  The
    standard edits endpoint supports 16 images and a mask; the legacy edit
    endpoint supports one image.  This is a total-body bound, not an in-memory
    allocation target: normal multipart parsing remains spooled by Starlette.
    """
    generic_max = int(limits.max_request_body_bytes)
    path = str(request.scope.get("path") or "")
    if path not in _COMPAT_IMAGE_EDIT_PATHS and path not in _SINGLE_IMAGE_EDIT_PATHS:
        return generic_max

    cfg = config.get()
    image_cfg = cfg.get("images") or {}
    max_image_bytes = _as_int(
        image_cfg.get("maxInputImageBytes"),
        20 * 1024 * 1024,
        minimum=1,
    )
    image_slots = (
        _OPENAI_MAX_EDIT_IMAGES + 1
        if path in _COMPAT_IMAGE_EDIT_PATHS
        else 1
    )
    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith("multipart/form-data"):
        per_image_wire_bytes = max_image_bytes
    else:
        # Exact base64 ceiling plus a small per-value allowance for the data URL
        # prefix, JSON quoting and separators.
        per_image_wire_bytes = 4 * ((max_image_bytes + 2) // 3) + 256
    endpoint_max = image_slots * per_image_wire_bytes + _IMAGE_BODY_ENVELOPE_BYTES
    return max(generic_max, endpoint_max)


class _BodyPreservingReceive:
    """Pure ASGI receive adapter for bounded queue-time replay.

    The owner passes the original ASGI ``receive`` callable explicitly. During
    queueing the disconnect watcher is its only consumer; after watcher shutdown,
    the owner passes ``receive`` below to the downstream application. No
    Starlette ``Request`` internals are read or replaced.
    """

    def __init__(self, request: Request, raw_receive: Receive,
                 key_name: str, limits: ResolvedLimits):
        self._request = request
        self._raw_receive = raw_receive
        self._key_name = key_name
        self._limits = limits
        self._buffer: deque[_BufferedBodyEvent] = deque()
        self._buffered_bytes = 0
        self._buffered_events = 0
        self._reserved_bytes = 0
        self._reserved_spool_bytes = 0
        self._spool_size = 0
        self._spool: Any | None = None
        self._spooled_to_disk = False
        self._guard_lock = asyncio.Lock()
        self._abandoned = False

    def validate_content_length(self) -> None:
        raw = self._request.headers.get("content-length")
        if raw is None:
            return
        try:
            content_length = int(raw)
        except (TypeError, ValueError):
            return
        limits = _resolve_limits(self._key_name)
        max_bytes = _request_body_max_bytes(self._request, limits)
        if content_length > max_bytes:
            raise RequestBodyTooLarge(max_bytes=max_bytes)

    def _account_body_event(self, message: Message, limits: ResolvedLimits) -> None:
        body_bytes = len(message.get("body", b""))
        next_body_bytes = self._buffered_bytes + body_bytes
        next_events = self._buffered_events + 1
        max_bytes = _request_body_max_bytes(self._request, limits)
        if next_body_bytes > max_bytes:
            raise RequestBodyTooLarge(max_bytes=max_bytes)
        if next_events > limits.max_request_body_events:
            raise RequestBodyTooLarge(
                max_bytes=max_bytes,
                max_events=limits.max_request_body_events,
                reason="event_count",
            )
        self._buffered_bytes = next_body_bytes
        self._buffered_events = next_events

    async def receive(self) -> Message:
        """Release accounting as each retained event is replayed, then go live."""
        async with self._guard_lock:
            if self._buffer:
                event = self._buffer.popleft()
                try:
                    body = await self._read_spooled_body(event)
                except Exception as exc:
                    await self._release_event_accounting(event)
                    if not self._buffer:
                        await self._close_spool()
                    raise _spool_failure("replay", exc) from exc
                await self._release_event_accounting(event)
                message = dict(event.message_without_body)
                if event.had_body:
                    message["body"] = body
                if not self._buffer:
                    await self._close_spool()
                return message
        message = await self._raw_receive()
        if message.get("type") == "http.request":
            limits = _resolve_limits(self._key_name)
            self._limits = limits
            self._account_body_event(message, limits)
        return message

    async def _reserve(self, message: Message) -> None:
        body_bytes = len(message.get("body", b""))
        # Body events may arrive throughout a long queue wait. Resolve the
        # effective budgets for every event so config hot reloads take effect.
        limits = _resolve_limits(self._key_name)
        async with self._guard_lock:
            if self._abandoned:
                return
            self._limits = limits
            self._account_body_event(message, limits)
            offset = self._spool_size
            next_spool_size = offset + body_bytes
            move_to_disk = (
                self._spooled_to_disk
                or next_spool_size > limits.queued_body_spool_threshold_bytes
            )
            existing_body_shift = (
                self._spool_size if move_to_disk and not self._spooled_to_disk else 0
            )
            memory_charge = QUEUED_BODY_EVENT_OVERHEAD_BYTES
            spool_charge = body_bytes if move_to_disk else 0
            if not move_to_disk:
                memory_charge += body_bytes

            await self._reserve_accounting(
                limits,
                memory_delta=memory_charge - existing_body_shift,
                spool_delta=spool_charge + existing_body_shift,
            )
            if existing_body_shift:
                for buffered in self._buffer:
                    buffered.memory_charge -= buffered.body_bytes
                    buffered.spool_charge += buffered.body_bytes
                self._reserved_bytes -= existing_body_shift
                self._reserved_spool_bytes += existing_body_shift
            self._reserved_bytes += memory_charge
            self._reserved_spool_bytes += spool_charge
            self._spool_size = next_spool_size
            self._spooled_to_disk = move_to_disk

            try:
                if body_bytes:
                    await self._write_spooled_body(
                        message.get("body", b""),
                        offset=offset,
                        limits=limits,
                        rollover=move_to_disk,
                    )
            except Exception as exc:
                raise _spool_failure("write", exc) from exc

            metadata = dict(message)
            had_body = "body" in metadata
            metadata.pop("body", None)
            self._buffer.append(_BufferedBodyEvent(
                message_without_body=metadata,
                had_body=had_body,
                offset=offset,
                body_bytes=body_bytes,
                memory_charge=memory_charge,
                spool_charge=spool_charge,
            ))

    async def _reserve_accounting(
        self,
        limits: ResolvedLimits,
        *,
        memory_delta: int,
        spool_delta: int,
    ) -> None:
        global _queued_body_bytes_total, _queued_body_spool_bytes_total
        request_max = _request_body_max_bytes(self._request, limits)
        one_request_memory = (
            min(request_max, limits.queued_body_spool_threshold_bytes)
            + limits.max_request_body_events * QUEUED_BODY_EVENT_OVERHEAD_BYTES
        )
        key_memory_budget = limits.max_queued_body_bytes
        if key_memory_budget == DEFAULT_MAX_QUEUED_BODY_BYTES_PER_KEY:
            key_memory_budget = max(key_memory_budget, one_request_memory)
        process_memory_budget = limits.max_queued_body_bytes_total
        if process_memory_budget == DEFAULT_MAX_QUEUED_BODY_BYTES_TOTAL:
            process_memory_budget = max(process_memory_budget, one_request_memory)
        key_spool_budget = limits.max_queued_body_spool_bytes
        if key_spool_budget == DEFAULT_MAX_QUEUED_BODY_SPOOL_BYTES_PER_KEY:
            key_spool_budget = max(key_spool_budget, request_max)
        process_spool_budget = limits.max_queued_body_spool_bytes_total
        if process_spool_budget == DEFAULT_MAX_QUEUED_BODY_SPOOL_BYTES_TOTAL:
            process_spool_budget = max(process_spool_budget, request_max)
        async with _queued_body_lock:
            key_memory = _queued_body_bytes_by_key.get(self._key_name, 0)
            key_spool = _queued_body_spool_bytes_by_key.get(self._key_name, 0)
            if key_memory + memory_delta > key_memory_budget:
                raise RequestBodyTooLarge(
                    max_bytes=key_memory_budget,
                    reason="key_aggregate",
                )
            if _queued_body_bytes_total + memory_delta > process_memory_budget:
                raise RequestBodyTooLarge(
                    max_bytes=process_memory_budget,
                    reason="process_aggregate",
                )
            if key_spool + spool_delta > key_spool_budget:
                raise RequestBodyTooLarge(
                    max_bytes=key_spool_budget,
                    reason="key_aggregate",
                )
            if _queued_body_spool_bytes_total + spool_delta > process_spool_budget:
                raise RequestBodyTooLarge(
                    max_bytes=process_spool_budget,
                    reason="process_aggregate",
                )
            _set_key_accounting(
                _queued_body_bytes_by_key,
                self._key_name,
                key_memory + memory_delta,
            )
            _set_key_accounting(
                _queued_body_spool_bytes_by_key,
                self._key_name,
                key_spool + spool_delta,
            )
            _queued_body_bytes_total += memory_delta
            _queued_body_spool_bytes_total += spool_delta

    def _open_spool(self, limits: ResolvedLimits) -> Any:
        directory = Path(config.DATA_DIR) / "queued-body-spool"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return tempfile.SpooledTemporaryFile(
            max_size=max(0, limits.queued_body_spool_threshold_bytes),
            mode="w+b",
            dir=directory,
            prefix="parrot-queued-body-",
        )

    async def _write_spooled_body(
        self,
        body: bytes,
        *,
        offset: int,
        limits: ResolvedLimits,
        rollover: bool,
    ) -> None:
        if self._spool is None:
            self._spool = self._open_spool(limits)

        def _write() -> None:
            assert self._spool is not None
            if rollover:
                self._spool.rollover()
            self._spool.seek(offset)
            written = self._spool.write(body)
            if written != len(body):
                raise OSError(f"short spool write: {written}/{len(body)}")
            self._spool.flush()

        if rollover:
            await self._run_blocking_io(_write)
        else:
            _write()

    async def _read_spooled_body(self, event: _BufferedBodyEvent) -> bytes:
        if event.body_bytes == 0:
            return b""
        if self._spool is None:
            raise OSError("queued body spool is closed")

        def _read() -> bytes:
            assert self._spool is not None
            self._spool.seek(event.offset)
            body = self._spool.read(event.body_bytes)
            if len(body) != event.body_bytes:
                raise OSError(
                    f"short spool read: {len(body)}/{event.body_bytes}"
                )
            return body

        if self._spooled_to_disk:
            return await self._run_blocking_io(_read)
        return _read()

    @staticmethod
    async def _run_blocking_io(func):
        """Let an in-flight fd operation finish before cancellation cleanup.

        ``asyncio.to_thread`` cancellation only abandons the await; it cannot
        stop the worker thread.  Waiting for that worker here prevents abandon()
        from closing/reusing the descriptor while a read/write is still active.
        """
        task = asyncio.create_task(asyncio.to_thread(func))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _release_event_accounting(self, event: _BufferedBodyEvent) -> None:
        global _queued_body_bytes_total
        async with _queued_body_lock:
            self._reserved_bytes -= event.memory_charge
            _queued_body_bytes_total -= event.memory_charge
            _set_key_accounting(
                _queued_body_bytes_by_key,
                self._key_name,
                _queued_body_bytes_by_key.get(self._key_name, 0) - event.memory_charge,
            )

    async def _close_spool(self) -> None:
        spool, self._spool = self._spool, None
        close_error: Exception | None = None
        try:
            if spool is not None:
                if self._spooled_to_disk:
                    await self._run_blocking_io(spool.close)
                else:
                    spool.close()
        except Exception as exc:
            close_error = exc
        finally:
            # A rolled temporary file keeps its full allocation until close;
            # releasing per-event disk accounting before this point would let
            # physical usage exceed the configured process/key budget.
            await self._release_spool_accounting()
        if close_error is not None:
            raise _spool_failure("close", close_error) from close_error

    async def _release_spool_accounting(self) -> None:
        global _queued_body_spool_bytes_total
        async with _queued_body_lock:
            spool_charge = self._reserved_spool_bytes
            self._reserved_spool_bytes = 0
            _queued_body_spool_bytes_total -= spool_charge
            _set_key_accounting(
                _queued_body_spool_bytes_by_key,
                self._key_name,
                _queued_body_spool_bytes_by_key.get(self._key_name, 0) - spool_charge,
            )

    async def wait_for_disconnect(self) -> None:
        """Consume raw events while preserving every request-body event."""
        while True:
            message = await self._raw_receive()
            message_type = message.get("type")
            if message_type == "http.disconnect":
                return
            if message_type != "http.request":
                continue
            # Once receive() yielded an event, finish its reservation even if
            # admission concurrently cancels this watcher; otherwise that body
            # chunk would be consumed without being replayable.
            reserve_task = asyncio.create_task(self._reserve(message))
            try:
                await asyncio.shield(reserve_task)
            except asyncio.CancelledError:
                await reserve_task
                raise

    async def abandon(self) -> None:
        """Release all remaining reservations exactly once."""
        global _queued_body_bytes_total
        async with self._guard_lock:
            if self._abandoned:
                return
            self._abandoned = True
            memory_charge = self._reserved_bytes
            self._reserved_bytes = 0
            self._buffer.clear()
            async with _queued_body_lock:
                _queued_body_bytes_total -= memory_charge
                _set_key_accounting(
                    _queued_body_bytes_by_key,
                    self._key_name,
                    _queued_body_bytes_by_key.get(self._key_name, 0) - memory_charge,
                )
            try:
                await self._close_spool()
            finally:
                _active_body_guards.discard(self)


def _set_key_accounting(mapping: dict[str, int], key_name: str, value: int) -> None:
    if value > 0:
        mapping[key_name] = value
    else:
        mapping.pop(key_name, None)


def _requires_nonqueued_body_guard(request: Request) -> bool:
    """Whether this endpoint has an explicit protocol-level body contract."""
    return str(request.scope.get("path") or "") in _MEDIA_PROTOCOL_BODY_PATHS


def _body_preserving_receive(
    request: Request,
    key_name: str,
    limits: ResolvedLimits,
    raw_receive: Receive | None = None,
) -> _BodyPreservingReceive:
    guard = _BodyPreservingReceive(
        request,
        raw_receive if raw_receive is not None else request.receive,
        key_name,
        limits,
    )
    guard.validate_content_length()
    _active_body_guards.add(guard)
    return guard


async def shutdown_spooling() -> None:
    """Close and unaccount all queued body stores during application shutdown."""
    guards = list(_active_body_guards)
    if guards:
        await asyncio.gather(
            *(guard.abandon() for guard in guards),
            return_exceptions=True,
        )


class ApiKeyLease:
    """一次 API Key slot 占用。release 幂等。"""

    def __init__(self, key_name: str, slot: ApiKeySlot | None,
                 limits: ResolvedLimits | None, *, queue_wait_ms: int = 0,
                 noop: bool = False,
                 replay_guard: _BodyPreservingReceive | None = None):
        self.key_name = key_name
        self.slot = slot
        self.limits = limits
        self.queue_wait_ms = int(queue_wait_ms)
        self.noop = noop
        self._replay_guard = replay_guard
        self._released = False
        self._release_task: asyncio.Task[None] | None = None
        self.attached_to_response = False

    @property
    def receive(self) -> Receive | None:
        """ASGI receive callable the owner must pass downstream, if wrapped."""
        return self._replay_guard.receive if self._replay_guard is not None else None

    @property
    def response_headers(self) -> dict[str, str]:
        """Limiter diagnostics to add to the downstream response start event."""
        return _headers(self.slot, self.queue_wait_ms)

    async def release(self) -> None:
        if self._released:
            return
        if self._release_task is None:
            self._release_task = asyncio.create_task(self._finish_release())
        # All callers await the same independently running cleanup. Cancelling
        # one caller cannot strand body accounting or the concurrency slot.
        await asyncio.shield(self._release_task)

    async def _finish_release(self) -> None:
        try:
            if self._replay_guard is not None:
                await self._replay_guard.abandon()
        finally:
            if not self.noop and self.slot is not None:
                await _release_slot(self.slot)
        self._released = True


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
    global_request_body_bytes = _as_int(
        global_cfg.get("defaultMaxRequestBodyBytes"),
        DEFAULT_MAX_REQUEST_BODY_BYTES,
        minimum=1,
    )
    global_request_body_events = _as_int(
        global_cfg.get("defaultMaxRequestBodyEvents"),
        DEFAULT_MAX_REQUEST_BODY_EVENTS,
        minimum=1,
    )
    global_queued_body_bytes = _as_int(
        global_cfg.get("defaultMaxQueuedBodyBytesPerKey"),
        DEFAULT_MAX_QUEUED_BODY_BYTES_PER_KEY,
        minimum=1,
    )
    process_queued_body_budget = global_cfg.get("maxQueuedBodyBytes")
    if process_queued_body_budget is None:
        # Backward compatibility for the pre-review private spelling.  The
        # documented/public key above always wins when both are present.
        process_queued_body_budget = global_cfg.get("maxQueuedBodyBytesTotal")
    max_queued_body_bytes_total = _as_int(
        process_queued_body_budget,
        DEFAULT_MAX_QUEUED_BODY_BYTES_TOTAL,
        minimum=1,
    )
    queued_body_spool_threshold_bytes = _as_int(
        global_cfg.get("queuedBodySpoolThresholdBytes"),
        DEFAULT_QUEUED_BODY_SPOOL_THRESHOLD_BYTES,
        minimum=0,
    )
    global_queued_body_spool_bytes = _as_int(
        global_cfg.get("defaultMaxQueuedBodySpoolBytesPerKey"),
        DEFAULT_MAX_QUEUED_BODY_SPOOL_BYTES_PER_KEY,
        minimum=1,
    )
    max_queued_body_spool_bytes_total = _as_int(
        global_cfg.get("maxQueuedBodySpoolBytes"),
        DEFAULT_MAX_QUEUED_BODY_SPOOL_BYTES_TOTAL,
        minimum=1,
    )
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

    max_request_body_bytes = _as_int(
        limits.get("maxRequestBodyBytes"),
        global_request_body_bytes,
        minimum=1,
    )
    max_request_body_events = _as_int(
        limits.get("maxRequestBodyEvents"),
        global_request_body_events,
        minimum=1,
    )
    max_queued_body_bytes = _as_int(
        limits.get("maxQueuedBodyBytes"),
        global_queued_body_bytes,
        minimum=1,
    )
    max_queued_body_spool_bytes = _as_int(
        limits.get("maxQueuedBodySpoolBytes"),
        global_queued_body_spool_bytes,
        minimum=1,
    )

    return ResolvedLimits(
        enabled=enabled,
        max_concurrent=max_concurrent,
        max_queue=max_queue,
        queue_wait_seconds=queue_wait,
        max_request_body_bytes=max_request_body_bytes,
        max_request_body_events=max_request_body_events,
        max_queued_body_bytes=max_queued_body_bytes,
        max_queued_body_bytes_total=max_queued_body_bytes_total,
        queued_body_spool_threshold_bytes=queued_body_spool_threshold_bytes,
        max_queued_body_spool_bytes=max_queued_body_spool_bytes,
        max_queued_body_spool_bytes_total=max_queued_body_spool_bytes_total,
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


async def acquire(
    key_name: str | None,
    request: Request | None = None,
    *, receive: Receive | None = None,
) -> ApiKeyLease:
    """占用一个 API Key 并发槽位；必要时进入 FIFO 队列。

    返回 ApiKeyLease，调用方必须在请求生命周期结束时 release。
    Queue disconnect monitoring preserves every unread ``http.request`` event
    and replays it to the route after admission.
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
        # Existing FIFO waiters get first claim on any newly available or
        # hot-reloaded capacity before this fresh request may use the fast path.
        _wake_eligible_waiters_locked(slot)
        if not limits.enabled:
            return ApiKeyLease(key, None, limits, noop=True)
        if limits.unlimited or slot.in_flight < limits.max_concurrent:
            # Generic replay limits are queue resource contracts, not protocol
            # caps for nonqueued core APIs. Image endpoints keep their separate,
            # endpoint-aware protocol body contract.
            replay_guard = None
            if request is not None and _requires_nonqueued_body_guard(request):
                replay_guard = _body_preserving_receive(
                    request, key, limits, raw_receive=receive,
                )
            slot.in_flight += 1
            return ApiKeyLease(
                key, slot, limits, queue_wait_ms=0, replay_guard=replay_guard
            )

        if limits.max_queue <= 0 or len(slot.waiters) >= limits.max_queue:
            raise _queue_full_error(key, slot)

        replay_guard = None
        if request is not None:
            replay_guard = _body_preserving_receive(
                request, key, limits, raw_receive=receive,
            )
        fut = asyncio.get_event_loop().create_future()
        waiter = Waiter(future=fut, enqueued_at=time.monotonic())
        slot.waiters.append(waiter)

    queue_deadline = start_wait + max(0, limits.queue_wait_seconds)
    try:
        await _wait_for_turn_or_abort(
            slot,
            waiter,
            replay_guard,
            limits,
            deadline=queue_deadline,
        )
        queue_wait_ms = int((time.monotonic() - start_wait) * 1000)
        # A normal wake-up owns a reserved capacity slot.  Keeping that
        # reservation in ``in_flight`` prevents a newly arriving request from
        # barging ahead of the FIFO waiter before it reacquires ``slot.lock``.
        while True:
            async with slot.lock:
                slot.limits = _resolve_limits(key)
                limits = slot.limits
                if waiter.capacity_reserved:
                    waiter.capacity_reserved = False
                    if not limits.enabled:
                        if slot.in_flight > 0:
                            slot.in_flight -= 1
                        _wake_eligible_waiters_locked(slot)
                        return ApiKeyLease(
                            key, None, limits, noop=True, replay_guard=replay_guard
                        )
                    if limits.unlimited:
                        _wake_eligible_waiters_locked(slot)
                    return ApiKeyLease(
                        key,
                        slot,
                        limits,
                        queue_wait_ms=queue_wait_ms,
                        replay_guard=replay_guard,
                    )
                if not limits.enabled:
                    _wake_eligible_waiters_locked(slot)
                    return ApiKeyLease(
                        key, None, limits, noop=True, replay_guard=replay_guard
                    )
                if limits.unlimited:
                    slot.in_flight += 1
                    _wake_eligible_waiters_locked(slot)
                    return ApiKeyLease(
                        key,
                        slot,
                        limits,
                        queue_wait_ms=queue_wait_ms,
                        replay_guard=replay_guard,
                    )
                if slot.in_flight < limits.max_concurrent:
                    slot.in_flight += 1
                    return ApiKeyLease(
                        key,
                        slot,
                        limits,
                        queue_wait_ms=queue_wait_ms,
                        replay_guard=replay_guard,
                    )
                if limits.max_queue <= 0 or len(slot.waiters) >= limits.max_queue:
                    raise _queue_full_error(key, slot)
                fut = asyncio.get_event_loop().create_future()
                waiter = Waiter(future=fut, enqueued_at=time.monotonic())
                slot.waiters.append(waiter)
            await _wait_for_turn_or_abort(
                slot,
                waiter,
                replay_guard,
                limits,
                deadline=queue_deadline,
            )
    except BaseException:
        try:
            await _drop_waiter(slot, waiter)
        finally:
            if replay_guard is not None:
                await replay_guard.abandon()
        raise


async def _wait_for_turn_or_abort(
    slot: ApiKeySlot,
    waiter: Waiter,
    replay_guard: _BodyPreservingReceive | None,
    limits: ResolvedLimits,
    *,
    deadline: float | None = None,
) -> None:
    timeout_seconds = limits.queue_wait_seconds
    if deadline is None:
        deadline = time.monotonic() + max(0, timeout_seconds)
    remaining_seconds = max(0.0, deadline - time.monotonic())
    wait_items: list[asyncio.Future | asyncio.Task] = [waiter.future]
    timeout_task: asyncio.Task | None = None
    disconnect_task: asyncio.Task | None = None
    limits_task: asyncio.Task | None = None
    if remaining_seconds <= 0:
        await _drop_waiter(slot, waiter)
        raise _timeout_error(slot.key_name, slot, timeout_seconds)
    timeout_task = asyncio.create_task(asyncio.sleep(remaining_seconds))
    wait_items.append(timeout_task)
    limits_task = asyncio.create_task(_wait_for_limit_relaxation(slot))
    wait_items.append(limits_task)
    if replay_guard is not None:
        disconnect_task = asyncio.create_task(_wait_http_disconnect(replay_guard))
        wait_items.append(disconnect_task)

    try:
        done, _pending = await asyncio.wait(wait_items, return_when=asyncio.FIRST_COMPLETED)
        if disconnect_task is not None and disconnect_task in done:
            await _drop_waiter(slot, waiter)
            disconnect_error = disconnect_task.exception()
            if disconnect_error is not None:
                raise disconnect_error
            raise ApiKeyLimitError(
                f"API key '{slot.key_name}' queued request was cancelled because the client disconnected.",
                reason="client_disconnected",
            )
        if waiter.future in done:
            return
        await _drop_waiter(slot, waiter)
        raise _timeout_error(slot.key_name, slot, timeout_seconds)
    finally:
        background_tasks = [
            item for item in wait_items
            if item is not waiter.future and isinstance(item, asyncio.Task)
        ]
        for task in background_tasks:
            if not task.done():
                task.cancel()
        if background_tasks:
            cleanup_results = await asyncio.gather(
                *background_tasks, return_exceptions=True
            )
            # A watcher cancelled by admission may have already consumed an
            # event whose reservation then exceeded a budget.  Do not hide it.
            for result in cleanup_results:
                if isinstance(result, (RequestBodyTooLarge, QueuedBodySpoolError)):
                    raise result


async def _wait_http_disconnect(guard: _BodyPreservingReceive) -> None:
    try:
        await guard.wait_for_disconnect()
    except (RequestBodyTooLarge, QueuedBodySpoolError):
        raise
    except Exception:
        # A closed/broken ASGI receive channel is equivalent to disconnect.
        return


def _pop_next_waiter_locked(slot: ApiKeySlot) -> Waiter | None:
    while slot.waiters:
        waiter = slot.waiters.pop(0)
        if not waiter.future.done():
            return waiter
    return None


def _wake_all_waiters_locked(slot: ApiKeySlot) -> None:
    """Wake every waiter without reserving finite capacity."""
    while True:
        waiter = _pop_next_waiter_locked(slot)
        if waiter is None:
            return
        waiter.future.set_result(None)


def _wake_eligible_waiters_locked(slot: ApiKeySlot) -> None:
    """Wake bypassed waiters or reserve each newly available finite slot."""
    limits = slot.limits
    if not limits.enabled or limits.unlimited:
        _wake_all_waiters_locked(slot)
        return
    available = max(0, limits.max_concurrent - slot.in_flight)
    while available > 0:
        waiter = _pop_next_waiter_locked(slot)
        if waiter is None:
            return
        waiter.capacity_reserved = True
        slot.in_flight += 1
        available -= 1
        waiter.future.set_result(None)


async def _wait_for_limit_relaxation(slot: ApiKeySlot) -> None:
    """Poll config while queued so hot-disable/unlimited changes wake waiters."""
    while True:
        await asyncio.sleep(0.5)
        try:
            latest = _resolve_limits(slot.key_name)
        except Exception:
            continue
        async with slot.lock:
            slot.limits = latest
            _wake_eligible_waiters_locked(slot)


async def _release_slot(slot: ApiKeySlot) -> None:
    async with slot.lock:
        slot.limits = _resolve_limits(slot.key_name)
        if slot.in_flight > 0:
            slot.in_flight -= 1
        _wake_eligible_waiters_locked(slot)


async def _drop_waiter(slot: ApiKeySlot, waiter: Waiter) -> None:
    async with slot.lock:
        try:
            slot.waiters.remove(waiter)
        except ValueError:
            pass
        if waiter.capacity_reserved:
            waiter.capacity_reserved = False
            if slot.in_flight > 0:
                slot.in_flight -= 1
            slot.limits = _resolve_limits(slot.key_name)
            _wake_eligible_waiters_locked(slot)
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
    if lease._released or (lease.noop and lease._replay_guard is None):
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
            # aclose() on this wrapper while suspended at yield does not close
            # the wrapped iterator automatically.  Propagate the close so the
            # upstream context and request-log terminal owner cannot be orphaned.
            try:
                aclose = getattr(body_iterator, "aclose", None)
                if callable(aclose):
                    try:
                        await aclose()
                    except Exception:
                        pass
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
