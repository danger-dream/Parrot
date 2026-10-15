"""Request log list, detail and protected body inspection controls."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any

from src import config as config_module
from src import log_db as log_db_module
from src import oauth_manager as oauth_manager_module
from src.management_auth import Capability
from src.management_control.context import ManagementContext
from src.management_control.errors import ManagementError, ManagementErrorCode

from . import inspector
from ._log_errors import map_historical_log_errors
from .common import (
    PageResult,
    camelize,
    normalize_utc_range,
    page_slice,
    require,
    revision_for,
    sanitize_credentials,
    utc_datetime,
)


class LogBodyKind(str, Enum):
    REQUEST = "request"
    RESPONSE = "response"


class RequestLogStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"
    PENDING = "pending"


class RequestProtocol(str, Enum):
    ANTHROPIC = "anthropic"
    CHAT = "chat"
    RESPONSES = "responses"
    RESPONSES_WS = "responses_ws"


class RequestLogSort(str, Enum):
    CREATED_AT = "createdAt"
    STATUS = "status"
    LATENCY = "latency"
    MODEL = "model"


@dataclass(frozen=True, slots=True)
class RequestLogQuery:
    statuses: tuple[RequestLogStatus, ...] = ()
    api_keys: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    channels: tuple[str, ...] = ()
    protocols: tuple[RequestProtocol, ...] = ()
    query: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    sort: RequestLogSort = RequestLogSort.CREATED_AT
    descending: bool = True
    page: int = 1
    page_size: int = 50


class BodySort(str, Enum):
    ORIGINAL = "original"
    REVERSE = "reverse"
    SIZE = "size"
    TYPE = "type"


@dataclass(frozen=True, slots=True)
class LogBodyPageResult:
    items: tuple[dict[str, Any], ...]
    page: int
    page_size: int
    total: int
    kind_counts: tuple[dict[str, Any], ...]
    revision: str

    @property
    def has_next(self) -> bool:
        return self.page * self.page_size < self.total


class LogsControl:
    def __init__(self, *, log_db=log_db_module, config=config_module, oauth_manager=oauth_manager_module) -> None:
        self.log_db = log_db
        self.config = config
        self.oauth_manager = oauth_manager

    def list_logs(self, context: ManagementContext, query: RequestLogQuery) -> PageResult[dict[str, Any]]:
        require(context)
        started_at, ended_at = normalize_utc_range(query.started_at, query.ended_at)
        query = replace(query, started_at=started_at, ended_at=ended_at)
        with map_historical_log_errors():
            rows, total = self.log_db.management_logs_page(
                statuses=[item.value for item in query.statuses] or None,
                api_keys=list(query.api_keys) or None,
                models=list(query.models) or None,
                channel_keys=list(query.channels) or None,
                protocols=[item.value for item in query.protocols] or None,
                query=query.query,
                started_at=(query.started_at.timestamp() if query.started_at is not None else None),
                ended_at=(query.ended_at.timestamp() if query.ended_at is not None else None),
                sort=query.sort.value,
                descending=query.descending,
                page=query.page,
                page_size=query.page_size,
            )
        billing = self.log_db.costs_for_logs(rows)
        return PageResult(
            tuple(
                self._list_record(row, billing=billing.get(str(row.get("request_id") or "")))
                for row in rows
            ),
            query.page,
            query.page_size,
            int(total),
        )

    def list_telegram(
        self,
        context: ManagementContext,
        *,
        limit: int,
        offset: int,
        api_keys: list[str] | None = None,
        models: list[str] | None = None,
        channel_keys: list[str] | None = None,
    ) -> tuple[list[dict], int]:
        """Exact current-store query used by the frozen TG paging adapter."""
        require(context)
        filters = {"api_keys": api_keys, "models": models, "channel_keys": channel_keys}
        total = int(self.log_db.recent_logs_count(**filters))
        rows = self.log_db.recent_logs(limit, offset=offset, **filters)
        return copy.deepcopy(rows), total

    def telegram_count(self, context: ManagementContext, **filters) -> int:
        require(context)
        return int(self.log_db.recent_logs_count(**filters))

    def telegram_recent(self, context: ManagementContext, *, limit: int, offset: int, **filters) -> list[dict]:
        require(context)
        return copy.deepcopy(self.log_db.recent_logs(limit, offset=offset, **filters))

    def recent_values(self, context: ManagementContext, kind: str) -> list[str]:
        require(context)
        return copy.deepcopy(self.log_db.recent_log_values(kind))

    def cost_for_log(self, context: ManagementContext, row: dict | None) -> dict:
        require(context)
        return copy.deepcopy(self.log_db.cost_for_log(row))

    @staticmethod
    def _billing_summary(value: Any) -> dict[str, int]:
        clean = sanitize_credentials(value if isinstance(value, dict) else {})
        return {
            "costTicks": int(clean.get("cost_ticks") or 0),
            "actualCostTicks": int(clean.get("actual_cost_ticks") or 0),
            "estimatedCostTicks": int(clean.get("estimated_cost_ticks") or 0),
            "actualCostedSuccess": int(clean.get("actual_costed_success") or 0),
            "estimatedCostedSuccess": int(clean.get("estimated_costed_success") or 0),
            "costedSuccess": int(clean.get("costed_success") or 0),
            "unpricedSuccess": int(clean.get("unpriced_success") or 0),
        }

    def _list_record(
        self,
        row: dict[str, Any],
        *,
        billing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean = sanitize_credentials(dict(row))
        billing = self._billing_summary(
            self.log_db.cost_for_log(row) if billing is None else billing
        )
        result = {
            "id": str(clean.get("request_id") or clean.get("id") or ""),
            "status": str(clean.get("status") or "unknown"),
            "createdAt": utc_datetime(clean.get("created_at")),
            "apiKeyName": clean.get("api_key_name"),
            "requestedModel": clean.get("requested_model"),
            "finalModel": clean.get("final_model"),
            "channelId": sanitize_credentials(row.get("final_channel_key")),
            "protocol": clean.get("protocol") or clean.get("ingress_protocol"),
            "transport": clean.get("upstream_transport"),
            "retryCount": int(clean.get("retry_count") or clean.get("total_retries") or 0),
            "durationMilliseconds": clean.get("duration_ms") or clean.get("total_time_ms"),
            "inputTokens": int(clean.get("input_tokens") or clean.get("prompt_tokens") or 0),
            "outputTokens": int(clean.get("output_tokens") or clean.get("completion_tokens") or 0),
            "costTicks": billing["costTicks"],
            "billing": billing,
            "error": clean.get("error_message"),
        }
        result["revision"] = revision_for(result)
        return result

    def filter_options(self, context: ManagementContext) -> dict[str, Any]:
        require(context)
        with map_historical_log_errors():
            result = copy.deepcopy(self.log_db.management_log_filter_options())
        result["revision"] = revision_for(result)
        return result

    def detail(self, context: ManagementContext, log_id: str) -> dict[str, Any]:
        require(context)
        with map_historical_log_errors():
            raw = self.log_db.management_log_detail(log_id)
        if not raw or not raw.get("log"):
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        detail = raw.get("detail") or {}
        log = raw.get("log") or {}
        result = {
            "id": log_id,
            "log": self._list_record(log),
            "stages": camelize(sanitize_credentials(raw.get("proxy_chain") or [])),
            "attempts": camelize(sanitize_credentials(raw.get("retry_chain") or [])),
            "localWebRounds": camelize(sanitize_credentials(raw.get("local_web_log") or [])),
            "billingAttempts": camelize(sanitize_credentials(raw.get("billing_attempts") or [])),
            "requestBodyAvailable": bool(detail.get("request_body")),
            "responseBodyAvailable": bool(detail.get("response_body")),
            "requestHeadersAvailable": bool(detail.get("request_headers")),
        }
        result["revision"] = revision_for(result)
        return result

    def raw_detail_for_telegram(self, context: ManagementContext, log_id: str) -> dict:
        require(context)
        return copy.deepcopy(self.log_db.log_detail(log_id))

    def _body_snapshot(self, context: ManagementContext, log_id: str, kind: LogBodyKind) -> tuple[Any, str]:
        require(context, Capability.LOG_BODY_READ)
        with map_historical_log_errors():
            raw = self.log_db.management_log_detail(log_id)
        if not raw or not raw.get("log"):
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        detail = raw.get("detail") or {}
        value = detail.get("request_body" if kind is LogBodyKind.REQUEST else "response_body")
        if value is None or value == "":
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        return value, self._list_record(raw["log"])["revision"]

    @staticmethod
    def _body_item_record(item: dict, log_id: str, kind: LogBodyKind, source_revision: str) -> dict:
        result = camelize(item)
        # Search bookkeeping is not part of the public body-item contract.
        result.pop("matchCount", None)
        result["id"] = f"item_{int(result.get('seq') or 0)}"
        result["revision"] = revision_for({
            "logId": log_id, "kind": kind.value,
            "itemId": result["id"], "sourceRevision": source_revision,
        })
        return result

    @staticmethod
    def _raw_business_body(value: Any) -> Any:
        """Return the stored business body without generic credential rewriting."""

        if isinstance(value, (dict, list)):
            return copy.deepcopy(value)
        text = str(value)
        try:
            return json.loads(text)
        except Exception:
            return text

    def body_items(
        self,
        context: ManagementContext,
        log_id: str,
        *,
        kind: LogBodyKind,
        query: str | None,
        sort: BodySort,
        item_kind: str | None,
        page: int,
        page_size: int,
    ) -> LogBodyPageResult:
        raw, source_revision = self._body_snapshot(context, log_id, kind)
        parser = (
            inspector.parse_request_body
            if kind is LogBodyKind.REQUEST else inspector.parse_response_body
        )
        searched = inspector.filter_items(parser(raw), query)
        counts: dict[str, int] = {}
        for item in searched:
            item_type = str(item.get("kind") or "")
            counts[item_type] = counts.get(item_type, 0) + 1
        items = searched
        if item_kind:
            items = [item for item in items if str(item.get("kind") or "") == item_kind]
        items = inspector.sort_items(items, sort.value)
        result = page_slice(items, page=page, page_size=page_size)
        return LogBodyPageResult(
            tuple(self._body_item_record(item, log_id, kind, source_revision) for item in result.items),
            page,
            page_size,
            result.total,
            tuple({"kind": key, "count": counts[key]} for key in sorted(counts)),
            revision_for({
                "logId": log_id, "kind": kind.value, "sourceRevision": source_revision,
                "query": query, "sort": sort.value, "itemKind": item_kind,
            }),
        )

    def body_item(
        self,
        context: ManagementContext,
        log_id: str,
        *,
        kind: LogBodyKind,
        item_id: str,
    ) -> dict[str, Any]:
        raw, source_revision = self._body_snapshot(context, log_id, kind)
        parser = (
            inspector.parse_request_body
            if kind is LogBodyKind.REQUEST else inspector.parse_response_body
        )
        items = parser(raw)
        try:
            seq = int(item_id.removeprefix("item_"))
        except (TypeError, ValueError):
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        for item in items:
            if int(item.get("seq") or 0) == seq:
                return self._body_item_record(item, log_id, kind, source_revision)
        raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)

    def raw_body(self, context: ManagementContext, log_id: str, *, kind: LogBodyKind) -> dict[str, Any]:
        raw, source_revision = self._body_snapshot(context, log_id, kind)
        return {
            "logId": log_id, "kind": kind.value, "body": self._raw_business_body(raw),
            "revision": revision_for({
                "logId": log_id, "kind": kind.value, "sourceRevision": source_revision,
            }),
        }

    def log_store_bodies(self, context: ManagementContext) -> bool:
        require(context)
        return self.config.get().get("logStoreBodies", False) is not False

    def configured_api_keys(self, context: ManagementContext) -> list[str]:
        require(context)
        keys = self.config.get().get("apiKeys") or {}
        return [str(key) for key in keys] if isinstance(keys, dict) else []

    def configured_channels(self, context: ManagementContext) -> list[str]:
        require(context)
        values: list[str] = []
        try:
            for channel in self.config.get().get("channels") or []:
                if not isinstance(channel, dict):
                    continue
                name = str(channel.get("name") or "").strip()
                if name:
                    values.append("api:" + name)
        except Exception:
            pass
        try:
            for account in self.oauth_manager.list_accounts():
                key = str(self.oauth_manager._account_key(account) or "")
                if key:
                    values.append("oauth:" + key)
        except Exception:
            pass
        return values


DEFAULT_LOGS_CONTROL = LogsControl()
