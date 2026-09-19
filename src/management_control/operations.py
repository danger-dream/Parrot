"""Bounded management Operation records and runtime-owned execution lifecycle."""

from __future__ import annotations

import asyncio
import copy
import secrets
import time
from collections import OrderedDict
from concurrent.futures import Future, wait
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from threading import Event, RLock
from typing import Any, Callable, Coroutine, Mapping

from src.management_auth.policy import CapabilityDenied, authorize
from src.management_auth.principal import Capability

from ._daemon_executor import DaemonBoundedExecutor
from .context import AuditSink, ManagementContext, audit_record
from .errors import ManagementError, ManagementErrorCode
from .public_safety import redact_known_fields


class OperationStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL = {
    OperationStatus.SUCCEEDED,
    OperationStatus.FAILED,
    OperationStatus.CANCELLED,
}

# Internal resource limits, deliberately not product/user configuration.  The
# former OAuth-only executor already used four workers; all Management
# operations now share that same process budget.
MANAGEMENT_OPERATION_MAX_WORKERS = 4
MANAGEMENT_OPERATION_SHUTDOWN_TIMEOUT_SECONDS = 5.0


def _coerce_public_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _coerce_public_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce_public_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _public_value(value: Any) -> Any:
    """Keep Operation results serializable after exact known-field redaction."""
    return _coerce_public_value(redact_known_fields(value))


@dataclass(frozen=True, slots=True)
class OperationProgress:
    current: int
    total: int
    message_code: str

    def __post_init__(self) -> None:
        if self.current < 0 or self.total < 0 or self.current > self.total:
            raise ValueError("invalid operation progress")
        if not self.message_code:
            raise ValueError("message_code must not be empty")


@dataclass(frozen=True, slots=True)
class OperationFailure:
    code: ManagementErrorCode
    message: str
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class ManagementOperation:
    id: str
    kind: str
    status: OperationStatus
    actor_subject_id: str
    session_id: str | None
    progress: OperationProgress | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    result: Any
    error: OperationFailure | None
    cancellable: bool


Clock = Callable[[], datetime]
OperationStarter = Callable[[str, ManagementContext, Any], None]


class OperationStore:
    """Thread-safe bounded records plus one owned worker/task lifecycle."""

    def __init__(
        self,
        *,
        max_operations: int = 500,
        clock: Clock | None = None,
        audit_sink: AuditSink | None = None,
        max_workers: int = MANAGEMENT_OPERATION_MAX_WORKERS,
        shutdown_timeout_seconds: float = MANAGEMENT_OPERATION_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        if max_operations < 1:
            raise ValueError("max_operations must be positive")
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        if shutdown_timeout_seconds < 0:
            raise ValueError("shutdown_timeout_seconds must not be negative")
        self._max_operations = max_operations
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._audit_sink = audit_sink
        self._items: OrderedDict[str, ManagementOperation] = OrderedDict()
        self._lock = RLock()
        self._executor = DaemonBoundedExecutor(
            max_workers=max_workers,
            max_queue_size=max_operations,
            thread_name_prefix="management-operation",
        )
        self._shutdown_timeout_seconds = float(shutdown_timeout_seconds)
        self._futures: dict[str, Future[None]] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._accepting = True
        self._closing = False
        self._closed = False
        self._close_complete = Event()

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("operation clock must return timezone-aware datetimes")
        return now

    @staticmethod
    def _authorize(context: ManagementContext, capability: Capability) -> None:
        try:
            authorize(context.actor, capability)
        except CapabilityDenied as exc:
            raise ManagementError(ManagementErrorCode.CAPABILITY_DENIED) from exc

    def _audit(self, context: ManagementContext, action: str, target: str, result: str) -> None:
        if self._audit_sink is not None:
            self._audit_sink.record(
                audit_record(
                    context,
                    action=action,
                    target=target,
                    result=result,
                    occurred_at=self._now(),
                )
            )

    def _make_room(self) -> None:
        while len(self._items) >= self._max_operations:
            terminal_id = next(
                (item_id for item_id, item in self._items.items() if item.status in _TERMINAL),
                None,
            )
            if terminal_id is None:
                raise ManagementError(
                    ManagementErrorCode.OPERATION_ALREADY_RUNNING,
                    retryable=True,
                )
            del self._items[terminal_id]

    def create(
        self,
        context: ManagementContext,
        *,
        kind: str,
        cancellable: bool,
    ) -> ManagementOperation:
        self._authorize(context, Capability.WRITE)
        if not kind or len(kind) > 120:
            raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)
        with self._lock:
            if not self._accepting:
                raise ManagementError(
                    ManagementErrorCode.SERVICE_NOT_READY,
                    retryable=True,
                )
            self._make_room()
            operation_id = f"op_{secrets.token_urlsafe(18)}"
            operation = ManagementOperation(
                id=operation_id,
                kind=kind,
                status=OperationStatus.QUEUED,
                actor_subject_id=context.actor.subject_id,
                session_id=context.actor.session_id,
                progress=None,
                created_at=self._now(),
                started_at=None,
                finished_at=None,
                result=None,
                error=None,
                cancellable=bool(cancellable),
            )
            self._items[operation_id] = operation
        self._audit(context, "operation.create", operation_id, "queued")
        return self._snapshot(operation)

    def _visible(self, context: ManagementContext, operation: ManagementOperation) -> bool:
        if operation.session_id is not None:
            return operation.session_id == context.actor.session_id
        return operation.actor_subject_id == context.actor.subject_id

    def _lookup_visible(self, context: ManagementContext, operation_id: str) -> ManagementOperation:
        operation = self._items.get(operation_id)
        if operation is None or not self._visible(context, operation):
            raise ManagementError(ManagementErrorCode.OPERATION_NOT_FOUND)
        return operation

    @staticmethod
    def _snapshot(operation: ManagementOperation) -> ManagementOperation:
        return replace(
            operation,
            result=copy.deepcopy(operation.result),
            error=copy.deepcopy(operation.error),
        )

    def get(self, context: ManagementContext, operation_id: str) -> ManagementOperation:
        self._authorize(context, Capability.READ)
        with self._lock:
            return self._snapshot(self._lookup_visible(context, operation_id))

    def mark_running(self, operation_id: str) -> ManagementOperation:
        with self._lock:
            operation = self._items.get(operation_id)
            if operation is None:
                raise ManagementError(ManagementErrorCode.OPERATION_NOT_FOUND)
            if operation.status is not OperationStatus.QUEUED:
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            operation = replace(
                operation,
                status=OperationStatus.RUNNING,
                started_at=self._now(),
            )
            self._items[operation_id] = operation
            return self._snapshot(operation)

    def seal_cancellation(self, operation_id: str) -> None:
        """Atomically cross an irreversible dispatch boundary, or reject cancellation.

        Only a running, not-cancelled worker can seal. A concurrent cancel wins
        before this lock or is refused afterward; neither path claims to undo a POST.
        """
        with self._lock:
            operation = self._items.get(operation_id)
            if operation is None or operation.status is not OperationStatus.RUNNING:
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            self._items[operation_id] = replace(operation, cancellable=False)

    def update_progress(
        self,
        operation_id: str,
        *,
        current: int,
        total: int,
        message_code: str,
    ) -> ManagementOperation:
        progress = OperationProgress(current=current, total=total, message_code=message_code)
        with self._lock:
            operation = self._items.get(operation_id)
            if operation is None:
                raise ManagementError(ManagementErrorCode.OPERATION_NOT_FOUND)
            if operation.status is not OperationStatus.RUNNING:
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            operation = replace(operation, progress=progress)
            self._items[operation_id] = operation
            return self._snapshot(operation)

    def succeed(self, operation_id: str, result: Any = None) -> ManagementOperation:
        with self._lock:
            operation = self._items.get(operation_id)
            if operation is None:
                raise ManagementError(ManagementErrorCode.OPERATION_NOT_FOUND)
            if operation.status not in {OperationStatus.QUEUED, OperationStatus.RUNNING}:
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            operation = replace(
                operation,
                status=OperationStatus.SUCCEEDED,
                result=_public_value(result),
                error=None,
                finished_at=self._now(),
                cancellable=False,
            )
            self._items[operation_id] = operation
            return self._snapshot(operation)

    def fail(
        self,
        operation_id: str,
        *,
        code: ManagementErrorCode,
        message: str,
        retryable: bool = False,
    ) -> ManagementOperation:
        with self._lock:
            operation = self._items.get(operation_id)
            if operation is None:
                raise ManagementError(ManagementErrorCode.OPERATION_NOT_FOUND)
            if operation.status not in {OperationStatus.QUEUED, OperationStatus.RUNNING}:
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            operation = replace(
                operation,
                status=OperationStatus.FAILED,
                result=None,
                # Worker exception text is intentionally not persisted: it may embed
                # credentials or upstream payloads.  The stable code is safe to expose.
                error=OperationFailure(code=code, message=code.value, retryable=retryable),
                finished_at=self._now(),
                cancellable=False,
            )
            self._items[operation_id] = operation
            return self._snapshot(operation)

    def cancel(self, context: ManagementContext, operation_id: str) -> None:
        self._authorize(context, Capability.WRITE)
        with self._lock:
            operation = self._lookup_visible(context, operation_id)
            if operation.status not in {OperationStatus.QUEUED, OperationStatus.RUNNING}:
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            if not operation.cancellable:
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            future = self._futures.get(operation_id)
            task = self._tasks.get(operation_id)
            if task is not None:
                task.get_loop().call_soon_threadsafe(task.cancel)
            self._items[operation_id] = replace(
                operation,
                status=OperationStatus.CANCELLED,
                finished_at=self._now(),
                cancellable=False,
            )
        # Future callbacks may release domain locks. Publish the terminal state
        # first and invoke them outside the store lock (domain -> store is the
        # submission lock order).
        if future is not None:
            future.cancel()
        self._audit(context, "operation.cancel", operation_id, "cancelled")

    def cancel_requested(self, operation_id: str) -> bool:
        """Cooperative cancellation probe for workers that support checkpoints."""
        with self._lock:
            operation = self._items.get(operation_id)
            return operation is not None and operation.status is OperationStatus.CANCELLED

    def interrupt_active(self) -> int:
        """Mark lifecycle-owned active records failed during orderly shutdown."""
        changed = 0
        with self._lock:
            for operation_id, operation in tuple(self._items.items()):
                if operation.status in {OperationStatus.QUEUED, OperationStatus.RUNNING}:
                    self._items[operation_id] = replace(
                        operation,
                        status=OperationStatus.FAILED,
                        result=None,
                        error=OperationFailure(
                            code=ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
                            message="Operation interrupted by service shutdown",
                            retryable=True,
                        ),
                        finished_at=self._now(),
                        cancellable=False,
                    )
                    changed += 1
        return changed

    def fail_if_active(
        self,
        operation_id: str,
        *,
        code: ManagementErrorCode,
        retryable: bool,
    ) -> None:
        with self._lock:
            operation = self._items.get(operation_id)
            if operation is None or operation.status not in {
                OperationStatus.QUEUED,
                OperationStatus.RUNNING,
            }:
                return
            self._items[operation_id] = replace(
                operation,
                status=OperationStatus.FAILED,
                result=None,
                error=OperationFailure(code=code, message=code.value, retryable=retryable),
                finished_at=self._now(),
                cancellable=False,
            )

    def _submission_error(
        self,
        operation_id: str,
        code: ManagementErrorCode,
    ) -> ManagementError:
        self.fail_if_active(operation_id, code=code, retryable=True)
        return ManagementError(code, retryable=True, operation_id=operation_id)

    def _run_owned(self, operation_id: str, worker: Callable[[], None]) -> None:
        try:
            worker()
        except BaseException:
            # Every owned execution must leave a terminal record.  Domain workers
            # normally map their own stable error; this is the last-resort path.
            self.fail_if_active(
                operation_id,
                code=ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
                retryable=True,
            )

    def _discard_future(self, operation_id: str, future: Future[None]) -> None:
        with self._lock:
            if self._futures.get(operation_id) is future:
                self._futures.pop(operation_id, None)

    def submit(self, operation_id: str, worker: Callable[[], None]) -> Future[None]:
        """Submit owned work; the future also signals cancellation before entry."""
        if not callable(worker):
            raise ValueError("worker must be callable")
        with self._lock:
            if not self._accepting:
                raise self._submission_error(
                    operation_id, ManagementErrorCode.SERVICE_NOT_READY,
                )
            operation = self._items.get(operation_id)
            if operation is None:
                raise ManagementError(ManagementErrorCode.OPERATION_NOT_FOUND)
            if operation.status is not OperationStatus.QUEUED:
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            if operation_id in self._futures or operation_id in self._tasks:
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            try:
                future = self._executor.submit(self._run_owned, operation_id, worker)
            except RuntimeError as exc:
                raise self._submission_error(
                    operation_id, ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
                ) from exc
            self._futures[operation_id] = future
            future.add_done_callback(
                lambda completed, oid=operation_id: self._discard_future(oid, completed)
            )
        return future

    def _task_done(self, operation_id: str, task: asyncio.Task[Any]) -> None:
        with self._lock:
            if self._tasks.get(operation_id) is task:
                self._tasks.pop(operation_id, None)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except (asyncio.CancelledError, RuntimeError):
            return
        if error is not None:
            self.fail_if_active(
                operation_id,
                code=ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
                retryable=True,
            )

    def create_task(self, operation_id: str, coroutine: Coroutine[Any, Any, Any]) -> None:
        """Retain an event-loop operation without converting it to a worker thread."""
        with self._lock:
            if not self._accepting:
                coroutine.close()
                raise self._submission_error(
                    operation_id, ManagementErrorCode.SERVICE_NOT_READY,
                )
            operation = self._items.get(operation_id)
            if operation is None:
                coroutine.close()
                raise ManagementError(ManagementErrorCode.OPERATION_NOT_FOUND)
            if operation.status is not OperationStatus.QUEUED:
                coroutine.close()
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            if operation_id in self._futures or operation_id in self._tasks:
                coroutine.close()
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            try:
                task = asyncio.get_running_loop().create_task(coroutine)
            except BaseException:
                coroutine.close()
                raise
            self._tasks[operation_id] = task
            task.add_done_callback(
                lambda completed, oid=operation_id: self._task_done(oid, completed)
            )

    def _begin_close(self) -> bool:
        with self._lock:
            if self._closed or self._closing:
                return False
            self._accepting = False
            self._closing = True
            futures = tuple(self._futures.values())
            # An asyncio Task whose record is still queued has not entered its
            # operation body. Running tasks get the same bounded grace period as
            # native workers.
            for operation_id, task in tuple(self._tasks.items()):
                operation = self._items.get(operation_id)
                if operation is not None and operation.status is OperationStatus.QUEUED:
                    task.cancel()
        # As in cancel(), never run domain completion callbacks under _lock.
        for future in futures:
            future.cancel()
        return True

    def _unfinished(self) -> tuple[tuple[Future[None], ...], tuple[asyncio.Task[Any], ...]]:
        with self._lock:
            futures = tuple(future for future in self._futures.values() if not future.done())
            tasks = tuple(task for task in self._tasks.values() if not task.done())
        return futures, tasks

    def _finish_close(self) -> int:
        futures, _tasks = self._unfinished()
        with self._lock:
            for task in tuple(self._tasks.values()):
                if not task.done():
                    task.cancel()
        interrupted = self.interrupt_active()
        # If every native task observed the grace period, join the fixed workers
        # now. A timed-out Python function cannot be force-stopped; its daemon
        # worker instead exits naturally later or with interpreter shutdown.
        self._executor.shutdown(wait=not futures, cancel_futures=True)
        with self._lock:
            self._closed = True
            self._closing = False
            self._close_complete.set()
        return interrupted

    def close(self, timeout_seconds: float | None = None) -> int:
        """Stop intake, cancel queued work, wait boundedly, then interrupt."""
        timeout = self._shutdown_timeout_seconds if timeout_seconds is None else max(0.0, timeout_seconds)
        if not self._begin_close():
            self._close_complete.wait(timeout + 0.1)
            return 0
        deadline = time.monotonic() + timeout
        futures, _tasks = self._unfinished()
        if futures:
            wait(futures, timeout=max(0.0, deadline - time.monotonic()))
        # Synchronous callers cannot drive event-loop tasks, but still honor the
        # same wall-time bound. Production lifespan uses aclose() below.
        while time.monotonic() < deadline:
            _futures, tasks = self._unfinished()
            if not _futures and not tasks:
                break
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        return self._finish_close()

    async def aclose(self, timeout_seconds: float | None = None) -> int:
        """Async shutdown variant that lets owned event-loop tasks make progress."""
        timeout = self._shutdown_timeout_seconds if timeout_seconds is None else max(0.0, timeout_seconds)
        if not self._begin_close():
            deadline = asyncio.get_running_loop().time() + timeout + 0.1
            while not self._close_complete.is_set() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            return 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            futures, tasks = self._unfinished()
            if not futures and not tasks:
                break
            await asyncio.sleep(min(0.01, max(0.0, deadline - loop.time())))
        _futures, tasks = self._unfinished()
        for task in tasks:
            task.cancel()
        if tasks:
            # Give cooperative asyncio cancellation one event-loop turn before
            # the runtime closes its backing state store.
            await asyncio.sleep(0)
        return self._finish_close()


class OperationRegistry:
    """Registers domain starters that submit into the runtime-owned lifecycle."""

    def __init__(self, store: OperationStore) -> None:
        self._store = store
        self._starters: dict[str, OperationStarter] = {}
        self._lock = RLock()

    def register(self, kind: str, starter: OperationStarter) -> None:
        if not kind or not callable(starter):
            raise ValueError("kind and callable starter are required")
        with self._lock:
            if kind in self._starters:
                raise ValueError(f"operation kind already registered: {kind}")
            self._starters[kind] = starter

    def create(
        self,
        context: ManagementContext,
        *,
        kind: str,
        payload: Any,
        cancellable: bool,
    ) -> ManagementOperation:
        with self._lock:
            starter = self._starters.get(kind)
        if starter is None:
            raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)
        operation = self._store.create(context, kind=kind, cancellable=cancellable)
        try:
            starter(operation.id, context, payload)
        except ManagementError as exc:
            self._store.fail_if_active(
                operation.id,
                code=exc.code,
                retryable=exc.retryable,
            )
            raise ManagementError(
                exc.code,
                exc.message,
                fields=exc.fields,
                retryable=exc.retryable,
                operation_id=operation.id,
            ) from exc
        except Exception as exc:
            self._store.fail_if_active(
                operation.id,
                code=ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
                retryable=True,
            )
            raise ManagementError(
                ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
                retryable=True,
                operation_id=operation.id,
            ) from exc
        return operation
