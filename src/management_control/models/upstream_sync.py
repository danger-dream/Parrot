"""Model-center upstream sync, owned by the existing OperationStore lifecycle.

A completed batch has a result status of succeeded/partial_failed/failed. The
Operation itself succeeds when that report was produced (not when every source
succeeded); Operation failure is reserved for interrupted/unexpected job failure.
"""

from __future__ import annotations

import asyncio
import copy
import time
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING

from src import config
from src.notifier import provider_label
from src.channel import registry
from src.management_control.channels.models import DiscoveryCommand
from src.management_control.context import ManagementContext
from src.management_control.errors import ManagementError, ManagementErrorCode
from src.management_control.operations import ManagementOperation

from .common import ModelSourceType, stable_revision

if TYPE_CHECKING:
    from .control import ModelCenterControl, ModelSourceRef


@dataclass(frozen=True)
class _Source:
    type: ModelSourceType
    id: str
    label: str
    revision: str

    @property
    def key(self) -> tuple[ModelSourceType, str]:
        return self.type, self.id


class UpstreamSync:
    """One coordinator per ModelCenterControl; no executor or persistent cache."""

    def __init__(self, control: ModelCenterControl) -> None:
        self.control = control
        self._lock = RLock()
        self._active: set[tuple[ModelSourceType, str]] = set()
        # 进度回调按 operation 登记：TG 菜单注册一个把状态 edit 回消息的 sink，
        # 任务每完成一步就调它一次。任务结束后立即移除，避免持着 chat 上下文。
        self._sinks: dict[str, object] = {}
        self._sinks_lock = RLock()

    def _oauth_label(self, account: dict) -> str:
        summary = self.control.oauth._summary(account)
        provider = summary.provider.value
        # Reuse the OAuth DTO's curated display/identity. Some legacy WorkBuddy
        # entries default label to uid; that is a routing identity, not a name.
        internal = {summary.account_id, summary.account_id.partition(":")[2]}
        if provider == "workbuddy":
            internal.add(str(account.get("uid") or ""))
        for field in ("email", "phone"):
            internal.discard(str(account.get(field) or ""))
        display = next((
            value for raw in (
                summary.display_name, account.get("display_name"), account.get("nickname"),
                account.get("email"), account.get("phone"), summary.identity,
            ) if (value := str(raw or "").strip()) and value not in internal
        ), "未命名账户")
        identity = str(summary.identity or "").strip()
        if identity and identity not in internal and identity != display:
            display = f"{display} · {identity}"
        return f"{provider_label(provider)} · {display}"

    def _sources(
        self,
        source: ModelSourceRef | None,
        refs: tuple[ModelSourceRef, ...] | None = None,
    ) -> tuple[_Source, ...]:
        if source is not None and source.type not in {ModelSourceType.API, ModelSourceType.OAUTH}:
            raise self.control._validation("source.type", "UNSUPPORTED_SCOPE", "source must be oauth or api")
        if refs is not None:
            for ref in refs:
                if ref.type not in {ModelSourceType.API, ModelSourceType.OAUTH}:
                    raise self.control._validation("source.type", "UNSUPPORTED_SCOPE", "source must be oauth or api")
        rows: dict[tuple[ModelSourceType, str], _Source] = {}
        # Enumerate containers, not model views: an empty catalog is syncable.
        for entry in config.get().get("channels") or ():
            name = str(entry.get("name") or "")
            if name:
                row = _Source(ModelSourceType.API, f"api:{name}", name, stable_revision(entry))
                rows[row.key] = row
        backend = self.control.oauth.backend
        for account in backend.list_accounts():
            account_id = backend.account_id(account)
            if account_id:
                row = _Source(
                    ModelSourceType.OAUTH, account_id,
                    self._oauth_label(account), stable_revision(account),
                )
                rows[row.key] = row
        if refs is not None:
            # 多选：保持调用方给出的顺序（界面上的顺序），并且必须是已知来源。
            # 去重但不静默丢弃未知项——未知 id 说明界面与配置已不一致。
            picked: list[_Source] = []
            seen: set[tuple[ModelSourceType, str]] = set()
            for ref in refs:
                row = rows.get((ref.type, ref.id))
                if row is None:
                    raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
                if row.key in seen:
                    continue
                seen.add(row.key)
                picked.append(row)
            if not picked:
                raise self.control._validation("sources", "EMPTY", "sources must not be empty")
            return tuple(picked)
        if source is None:
            return tuple(rows.values())
        row = rows.get((source.type, source.id))
        if row is None:
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        return (row,)

    def start(
        self,
        context: ManagementContext,
        source: ModelSourceRef | None,
        refs: tuple[ModelSourceRef, ...] | None = None,
        progress_sink=None,
    ) -> ManagementOperation:
        store = self.control.operations
        with self._lock:
            sources = self._sources(source, refs)
            keys = {row.key for row in sources}
            if self._active.intersection(keys):
                # Reject rather than expose a different actor's operation record.
                raise ManagementError(ManagementErrorCode.OPERATION_ALREADY_RUNNING, retryable=True)
            operation = store.create(context, kind="model_center.upstream.sync", cancellable=True)
            if progress_sink is not None:
                with self._sinks_lock:
                    self._sinks[operation.id] = progress_sink
            self._active.update(keys)
            try:
                store.submit(operation.id, lambda: self._run(operation.id, context, sources))
            except BaseException:
                self._active.difference_update(keys)
                with self._sinks_lock:
                    self._sinks.pop(operation.id, None)
                raise
        self.control._audit(context, "model_center.upstream.sync", operation.id, "queued")
        return operation

    def _current(self, source: _Source) -> dict:
        if source.type is ModelSourceType.API:
            current = next((
                row for row in config.get().get("channels") or ()
                if f"api:{row.get('name')}" == source.id
            ), None)
        else:
            current = self.control.oauth.backend.get_account_exact(source.id)
        if current is None:
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        if stable_revision(current) != source.revision:
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        return copy.deepcopy(current)

    async def _api(self, context: ManagementContext, source: _Source) -> dict:
        self._current(source)
        discovered = await self.control.channels.discover_model_ids(
            context, DiscoveryCommand(channel_id=source.id),
        )
        # Preset static/error fallbacks are not successful live catalog syncs.
        if discovered.source != "live":
            raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)
        if discovered.error or not discovered.models:
            raise ManagementError(ManagementErrorCode.UPSTREAM_ERROR)
        counts = {}

        def append_models(cfg: dict) -> None:
            entry = next((
                row for row in cfg.get("channels") or ()
                if f"api:{row.get('name')}" == source.id
            ), None)
            if entry is None:
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
            if stable_revision(entry) != source.revision:
                raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
            existing = list(entry.get("models") or [])
            real_ids = {
                str(row.get("real") or "") if isinstance(row, dict) else str(row)
                for row in existing
            }
            aliases = {
                str(row.get("alias") or row.get("real") or "") if isinstance(row, dict) else str(row)
                for row in existing
            }
            added = skipped = 0
            for model in dict.fromkeys(discovered.models):
                if model in real_ids:
                    continue
                # Never add a duplicate public alias or retarget a manual alias.
                if model in aliases:
                    skipped += 1
                    continue
                existing.append({"real": model, "alias": model})
                real_ids.add(model)
                aliases.add(model)
                added += 1
            if added:
                entry["models"] = existing
            counts.update(count=len(existing), addedCount=added, skippedCount=skipped)

        with config.serialized_updates():
            with config.observe_reload_failures() as failures:
                config.update(append_models, skip_if_unchanged=True)
                # Also cover controls composed without the server's reload hook.
                registry.rebuild_from_config()
            if failures:
                raise ManagementError(ManagementErrorCode.DEPENDENCY_UNAVAILABLE)
        return counts

    async def _oauth(self, source: _Source) -> dict:
        self._current(source)
        # Use the same authoritative refresh as OAuthControl.sync_models, inline
        # in this owned worker. Nested OperationStore workers would deadlock when
        # concurrent batches occupied all four workers waiting for child jobs.
        with config.observe_reload_failures() as failures:
            result = await self.control.oauth.backend.refresh_account_models(source.id)
        action = str((result or {}).get("action") or "error")
        if action not in {"updated", "not_modified"}:
            code = {
                "stale": ManagementErrorCode.REVISION_CONFLICT,
                "timeout": ManagementErrorCode.UPSTREAM_TIMEOUT,
                "network_disabled": ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
            }.get(action, ManagementErrorCode.UPSTREAM_ERROR)
            raise ManagementError(code)
        if failures:
            raise ManagementError(ManagementErrorCode.DEPENDENCY_UNAVAILABLE)
        return {"count": int(result.get("models") or 0)}

    def _emit(self, operation_id: str, event: dict) -> None:
        """把一个进度事件交给该任务的 sink；sink 是可选增强，出错不影响同步。"""
        with self._sinks_lock:
            sink = self._sinks.get(operation_id)
        if sink is None:
            return
        try:
            sink(operation_id, event)
        except Exception:
            pass

    async def _batch(self, operation_id: str, context: ManagementContext, sources: tuple[_Source, ...]) -> dict:
        store = self.control.operations
        items = []
        store.update_progress(operation_id, current=0, total=len(sources), message_code="model_center.upstream.sync.running")
        cancelled = False
        for index, source in enumerate(sources):
            # 协作式取消：只在两项之间检查，已经发出的上游请求不中断。
            if store.cancel_requested(operation_id):
                cancelled = True
                break
            self._emit(operation_id, {
                "phase": "start", "index": index, "total": len(sources),
                "sourceType": source.type.value, "label": source.label,
            })
            row = {
                "status": "succeeded", "sourceType": source.type.value,
                "sourceId": source.id, "label": source.label, "count": 0, "errorCode": None,
                "models": [],
            }
            started = time.monotonic()
            try:
                counts = await (self._api(context, source) if source.type is ModelSourceType.API else self._oauth(source))
                row.update(counts)
                if counts.get("skippedCount"):
                    row.update(status="partial_failed", errorCode="ALIAS_CONFLICT")
            except ManagementError as exc:
                row.update(status="failed", errorCode=exc.code.value)
            except Exception:
                # Never publish exceptions, upstream bodies, tokens or URLs.
                row.update(status="failed", errorCode=ManagementErrorCode.UPSTREAM_ERROR.value)
            row["elapsedMs"] = int((time.monotonic() - started) * 1000)
            row["models"] = self._source_model_names(source)
            items.append(row)
            try:
                store.update_progress(operation_id, current=len(items), total=len(sources), message_code="model_center.upstream.sync.running")
            except ManagementError:
                # 状态已不是 RUNNING，说明任务被取消或服务关闭中断了。
                # 这是本循环唯一的终止信号：继续跑后续来源会在关闭后仍发起上游
                # 请求（原来正是靠这个异常中断的）。因此必须向上抛出。
                # 抛之前补发一次取消事件，进度页才能从"正在同步"切到"已取消"。
                if store.cancel_requested(operation_id):
                    self._emit(operation_id, {
                        "phase": "cancelled", "index": index, "total": len(sources),
                        "sourceType": source.type.value, "label": source.label,
                    })
                raise
            self._emit(operation_id, {
                "phase": "done", "index": index, "total": len(sources),
                "sourceType": source.type.value, "label": source.label,
                "status": row["status"], "errorCode": row["errorCode"],
                "count": int(row.get("count") or 0),
                "models": list(row["models"]), "elapsedMs": row["elapsedMs"],
            })
        if cancelled:
            cancelled_row = {
                "status": "cancelled", "sourceType": sources[len(items)].type.value,
                "sourceId": sources[len(items)].id, "label": sources[len(items)].label,
                "count": 0, "errorCode": None, "models": [], "elapsedMs": 0,
            }
            items.append(cancelled_row)
            self._emit(operation_id, {
                "phase": "cancelled", "index": len(items) - 1, "total": len(sources),
                "sourceType": cancelled_row["sourceType"], "label": cancelled_row["label"],
            })
            return {
                "status": "cancelled", "total": len(sources), "succeeded": 0,
                "failed": 0, "items": items,
            }
        succeeded = sum(row["status"] == "succeeded" for row in items)
        failed = len(items) - succeeded
        status = "succeeded" if not failed else "partial_failed" if succeeded or any(row["status"] == "partial_failed" for row in items) else "failed"
        store.update_progress(operation_id, current=len(items), total=len(sources), message_code=f"model_center.upstream.sync.{status}")
        return {"status": status, "total": len(items), "succeeded": succeeded, "failed": failed, "items": items}

    def _source_model_names(self, source: _Source) -> list[str]:
        """该来源同步后的模型名（已排序）；供进度页展示前几个。"""
        try:
            if source.type is ModelSourceType.API:
                entry = next((
                    row for row in config.get().get("channels") or ()
                    if f"api:{row.get('name')}" == source.id
                ), None)
                names = [
                    str(row.get("alias") or row.get("real") or "") if isinstance(row, dict) else str(row)
                    for row in (entry or {}).get("models") or ()
                ]
            else:
                account = self.control.oauth.backend.get_account_exact(source.id) or {}
                names = [str(name) for name in account.get("models") or ()]
        except Exception:
            return []
        return sorted({name for name in names if name})

    def _run(self, operation_id: str, context: ManagementContext, sources: tuple[_Source, ...]) -> None:
        store = self.control.operations
        try:
            store.mark_running(operation_id)
            result = asyncio.run(self._batch(operation_id, context, sources))
            if result["status"] == "cancelled":
                self.control._audit(context, "model_center.upstream.sync", operation_id, "cancelled")
            else:
                store.succeed(operation_id, result)
                self.control._audit(context, "model_center.upstream.sync", operation_id, result["status"])
        finally:
            with self._sinks_lock:
                self._sinks.pop(operation_id, None)
            with self._lock:
                self._active.difference_update(row.key for row in sources)
