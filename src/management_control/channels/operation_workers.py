"""Channel-domain operation workers; public action APIs remain in service."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any
from ..context import ManagementContext
from ..errors import ManagementError, ManagementErrorCode
from .models import DiscoveryCommand, DraftProbeCommand


class ChannelOperationWorkers:
    def _retain_task(self, operation_id: str, coroutine: Any) -> None:
        assert self._operation_store is not None
        self._operation_store.create_task(operation_id, coroutine)

    def _start_discovery_owner(self, operation_id: str, context: ManagementContext, payload: Any) -> None:
        self._retain_task(
            operation_id,
            self._run_discovery_operation(operation_id, context, payload),
        )

    async def _run_discovery_operation(
        self, operation_id: str, context: ManagementContext, command: DiscoveryCommand
    ) -> None:
        assert self._operation_store is not None
        self._operation_store.mark_running(operation_id)
        try:
            result = await self.discover_model_ids(context, command)
            if not result.models:
                self._operation_store.fail(
                    operation_id, code=ManagementErrorCode.UPSTREAM_ERROR,
                    message=ManagementErrorCode.UPSTREAM_ERROR.value, retryable=result.retry_available,
                )
            else:
                self._operation_store.succeed(operation_id, asdict(result))
        except ManagementError as exc:
            self._operation_store.fail(
                operation_id, code=exc.code, message=exc.code.value, retryable=exc.retryable
            )
        except Exception:
            self._operation_store.fail(
                operation_id, code=ManagementErrorCode.UPSTREAM_ERROR,
                message=ManagementErrorCode.UPSTREAM_ERROR.value, retryable=True,
            )

    def _start_draft_probe_owner(self, operation_id: str, context: ManagementContext, payload: Any) -> None:
        self._retain_task(
            operation_id,
            self._run_draft_probe_operation(operation_id, context, payload),
        )

    async def _run_draft_probe_operation(
        self, operation_id: str, context: ManagementContext, command: DraftProbeCommand
    ) -> None:
        assert self._operation_store is not None
        self._operation_store.mark_running(operation_id)
        try:
            result = await self.probe_draft(context, command)
            public = asdict(result)
            public["reason"] = None if result.ok else "PROBE_FAILED"
            self._operation_store.succeed(operation_id, public)
        except ManagementError as exc:
            self._operation_store.fail(operation_id, code=exc.code, message=exc.code.value)
        except Exception:
            self._operation_store.fail(
                operation_id, code=ManagementErrorCode.UPSTREAM_ERROR,
                message=ManagementErrorCode.UPSTREAM_ERROR.value, retryable=True,
            )

    def _start_diagnostic_owner(self, operation_id: str, context: ManagementContext, payload: Any) -> None:
        self._retain_task(
            operation_id,
            self._run_diagnostic_operation(operation_id, context, payload),
        )

    async def _run_diagnostic_operation(
        self, operation_id: str, context: ManagementContext, payload: dict[str, str]
    ) -> None:
        assert self._operation_store is not None
        self._operation_store.mark_running(operation_id)
        try:
            result = await self.probe_existing(
                context, payload["channel_id"], payload["model"]
            )
            public = asdict(result)
            public["reason"] = None if result.ok else "PROBE_FAILED"
            self._operation_store.succeed(operation_id, public)
        except ManagementError as exc:
            self._operation_store.fail(operation_id, code=exc.code, message=exc.code.value)
        except Exception:
            self._operation_store.fail(
                operation_id, code=ManagementErrorCode.UPSTREAM_ERROR,
                message=ManagementErrorCode.UPSTREAM_ERROR.value, retryable=True,
            )

    def _start_usage_owner(self, operation_id: str, context: ManagementContext, payload: Any) -> None:
        assert self._operation_store is not None
        self._operation_store.mark_running(operation_id)
        try:
            result = self.schedule_provider_usage(context, payload["channel_id"], force=True)
            self._operation_store.succeed(operation_id, asdict(result))
        except ManagementError as exc:
            self._operation_store.fail(operation_id, code=exc.code, message=exc.code.value)
        except Exception:
            self._operation_store.fail(
                operation_id, code=ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
                message=ManagementErrorCode.DEPENDENCY_UNAVAILABLE.value, retryable=True,
            )
