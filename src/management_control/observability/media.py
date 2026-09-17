"""Multimedia log queries and authenticated cached-artifact access."""

from __future__ import annotations

import copy
import heapq
import hashlib
import json
import mimetypes
import os
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any, Iterator

from src import config as config_module
from src import media_cache, media_config
from src import media_db as media_db_module
from src.management_control.context import ManagementContext
from src.management_control.errors import ManagementError, ManagementErrorCode

from .common import (
    PageResult,
    normalize_utc_range,
    require,
    revision_for,
    sanitize_credentials,
    utc_datetime,
)


class MediaStatus(str, Enum):
    RUNNING = "running"
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class MediaAction(str, Enum):
    GENERATE = "generate"
    EDIT = "edit"
    EXTEND = "extend"


class MediaSort(str, Enum):
    CREATED_AT = "createdAt"
    STATUS = "status"
    COST = "cost"
    DURATION = "duration"


@dataclass(frozen=True, slots=True)
class MediaLogQuery:
    statuses: tuple[MediaStatus, ...] = ()
    providers: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    actions: tuple[MediaAction, ...] = ()
    started_at: datetime | None = None
    ended_at: datetime | None = None
    sort: MediaSort = MediaSort.CREATED_AT
    descending: bool = True
    page: int = 1
    page_size: int = 50


@dataclass(frozen=True, slots=True)
class ArtifactDownload:
    filename: str
    content_type: str
    size: int
    chunks: Iterator[bytes]


_SCAN_CHUNK = 200


class MediaControl:
    def __init__(self, *, media_db=media_db_module, config=config_module) -> None:
        self.media_db = media_db
        self.config = config

    def _paths(
        self, row: dict[str, Any], *, existing_only: bool = True,
        enforce_root: bool = True,
    ) -> list[str]:
        try:
            values = json.loads(row.get("cache_paths") or "[]")
        except Exception:
            values = []
        if not isinstance(values, list):
            return []
        paths = [path for path in values if isinstance(path, str)]
        if not existing_only:
            return paths
        root = self.config.get()
        images = media_config.settings("video" if row.get("media_type") == "video" else "image", root) if isinstance(root, dict) and ("images" in root or "videos" in root) else None
        # Test/legacy adapters without cache settings retain metadata-only behavior;
        # production config always has images settings and enforces containment.
        if (
            not enforce_root
            or not isinstance(images, dict)
            or "cachePath" not in images
        ):
            return [path for path in paths if os.path.isfile(path) and not os.path.islink(path)]
        return [path for path in paths if media_cache.artifact_path_is_safe(path, images)]

    @staticmethod
    def _artifact_id(index: int, path: str) -> str:
        digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:12]
        return f"artifact_{index}_{digest}"

    @staticmethod
    def _record(row: dict[str, Any]) -> dict[str, Any]:
        clean = sanitize_credentials(dict(row))
        try:
            output_sizes = json.loads(clean.get('output_sizes') or 'null')
        except (TypeError, ValueError):
            output_sizes = None
        if not isinstance(output_sizes, list) or not all(isinstance(item, str) for item in output_sizes):
            output_sizes = None
        result = {
            "id": str(clean.get("id") or ""),
            "requestId": str(clean.get("request_id") or ""),
            "status": str(clean.get("status") or "pending"),
            "provider": str(clean.get("provider") or "unknown"),
            "model": str(clean.get("model") or clean.get("tool_model") or clean.get("main_model") or ""),
            "action": str(clean.get("action") or "generate"),
            "mediaType": str(clean.get("media_type") or "image"),
            "progress": clean.get("progress"),
            "aspectRatio": clean.get("aspect_ratio"),
            "resolution": clean.get("resolution") or clean.get("size"),
            "durationSeconds": clean.get("media_duration_seconds"),
            "durationMilliseconds": clean.get("duration_ms"),
            "costTicks": int(clean.get("cost_usd_ticks") or 0),
            "trafficBytes": int(clean['image_bytes']) if clean.get('image_bytes') and int(clean['image_bytes']) > 0 else None,
            "generatedCount": int(clean['image_count']) if clean.get('image_count') and int(clean['image_count']) > 0 else None,
            "outputSizes": output_sizes,
            "cacheStatus": str(clean.get("cache_status") or "") or None,
            "cacheErrorClass": str(clean.get("cache_error_class") or "") or None,
            "createdAt": utc_datetime(clean.get("created_at")),
            "finishedAt": utc_datetime(clean.get("finished_at")),
            "error": clean.get("error_message"),
        }
        # Resource revisions are derived only from fields the public DTO emits;
        # hidden DB columns (including credentials) cannot perturb the token.
        result["revision"] = revision_for(result)
        return result

    @staticmethod
    def _matches(row: dict[str, Any], query: MediaLogQuery) -> bool:
        if query.statuses and str(row.get("status") or "") not in {item.value for item in query.statuses}:
            return False
        if query.providers and str(row.get("provider") or "") not in query.providers:
            return False
        model = str(row.get("model") or row.get("tool_model") or row.get("main_model") or "")
        if query.models and model not in query.models:
            return False
        if query.actions and str(row.get("action") or "") not in {item.value for item in query.actions}:
            return False
        created = utc_datetime(row.get("created_at"))
        if query.started_at is not None and (created is None or created < query.started_at):
            return False
        if query.ended_at is not None and (created is None or created > query.ended_at):
            return False
        return True

    @staticmethod
    def _sort_value(row: dict[str, Any], sort: MediaSort) -> Any:
        if sort is MediaSort.STATUS:
            return str(row.get("status") or "")
        if sort is MediaSort.COST:
            return int(row.get("cost_usd_ticks") or 0)
        if sort is MediaSort.DURATION:
            return float(row.get("duration_ms") or 0)
        created = utc_datetime(row.get("created_at"))
        return created.timestamp() if created else 0.0

    def list_logs(self, context: ManagementContext, query: MediaLogQuery) -> PageResult[dict[str, Any]]:
        require(context)
        started_at, ended_at = normalize_utc_range(query.started_at, query.ended_at)
        query = replace(query, started_at=started_at, ended_at=ended_at)
        total_candidates = int(self.media_db.count())
        is_default = not (
            query.statuses or query.providers or query.models or query.actions
            or query.started_at is not None or query.ended_at is not None
            or query.sort is not MediaSort.CREATED_AT or not query.descending
        )
        if is_default:
            rows = self.media_db.recent(
                query.page_size, offset=(query.page - 1) * query.page_size,
            )
            return PageResult(
                tuple(self._record(row) for row in rows),
                query.page, query.page_size, total_candidates,
            )

        matched = 0

        def candidates():
            nonlocal matched
            for offset in range(0, total_candidates, _SCAN_CHUNK):
                chunk = self.media_db.recent(
                    min(_SCAN_CHUNK, total_candidates - offset), offset=offset,
                )
                if not chunk:
                    break
                for row in chunk:
                    if self._matches(row, query):
                        matched += 1
                        yield row

        top_k = query.page * query.page_size
        selector = heapq.nlargest if query.descending else heapq.nsmallest
        selected = selector(
            top_k, candidates(), key=lambda row: self._sort_value(row, query.sort),
        )
        start = (query.page - 1) * query.page_size
        return PageResult(
            tuple(self._record(row) for row in selected[start:start + query.page_size]),
            query.page, query.page_size, matched,
        )

    def telegram_page(self, context: ManagementContext, *, page: int, page_size: int) -> tuple[list[dict], dict, int, int]:
        require(context)
        total = int(self.media_db.count())
        pages = max(1, (max(0, total) + page_size - 1) // page_size)
        try:
            normalized = int(page or 1)
        except (TypeError, ValueError):
            normalized = 1
        normalized = max(1, min(normalized, pages))
        rows = self.media_db.recent(page_size, offset=(normalized - 1) * page_size)
        return copy.deepcopy(rows), copy.deepcopy(self.media_db.summary()), normalized, pages

    def account_top(self, context: ManagementContext, *, limit: int = 3) -> list[dict]:
        require(context)
        active_keys: set[str] = set()
        from src.oauth_ids import account_key
        for account in self.config.get().get("oauthAccounts", []):
            if isinstance(account, dict):
                key = account_key(account)
                if key:
                    active_keys.add(key)
        current = []
        for row in self.media_db.account_top(1000):
            if str(row.get("account_key") or "") in active_keys:
                current.append(copy.deepcopy(row))
                if len(current) >= limit:
                    break
        return current

    def raw_log_for_telegram(self, context: ManagementContext, log_id: int) -> dict | None:
        require(context)
        row = self.media_db.get_log(int(log_id))
        return copy.deepcopy(row) if row else None

    def detail(self, context: ManagementContext, media_log_id: str) -> dict[str, Any]:
        require(context)
        try:
            numeric_id = int(media_log_id)
        except (TypeError, ValueError):
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        row = self.media_db.get_log(numeric_id)
        if not row:
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        clean = sanitize_credentials(dict(row))
        paths = self._paths(row)
        result = self._record(clean)
        result.pop("revision", None)
        result.update({
            # ``account_key`` is the media DB's account identifier, not a credential;
            # sanitize its free-text value without treating the storage column name
            # as an untrusted public payload key.
            "accountId": str(sanitize_credentials(row.get("account_key")) or "") or None,
            "accountLabel": str(clean.get("account_email") or "") or None,
            "upstreamRequestId": str(clean.get("upstream_request_id") or "") or None,
            "upstreamStatus": str(clean.get("upstream_status") or "") or None,
            "httpStatus": int(clean.get("http_status") or 0) or None,
            "promptPreview": str(clean.get("prompt_preview") or "") or None,
            "artifactCount": len(paths),
            "paths": [sanitize_credentials(os.path.basename(path)) for path in paths],
        })
        result["revision"] = revision_for(result)
        return result

    def artifacts(self, context: ManagementContext, media_log_id: str) -> list[dict[str, Any]]:
        require(context)
        try:
            row = self.media_db.get_log(int(media_log_id))
        except (TypeError, ValueError):
            row = None
        if not row or str(row.get("status") or "").strip().lower() == MediaStatus.EXPIRED.value:
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        out = []
        for index, path in enumerate(self._paths(row), 1):
            content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
            artifact = {
                "id": self._artifact_id(index, path),
                "fileName": os.path.basename(path),
                "contentType": content_type,
                "sizeBytes": os.path.getsize(path),
                "mediaType": "video" if content_type.startswith("video/") else "image",
                "expiresAt": utc_datetime(row.get("expires_at")),
            }
            artifact["revision"] = revision_for(artifact)
            out.append(artifact)
        return out

    def download(self, context: ManagementContext, media_log_id: str, artifact_id: str) -> ArtifactDownload:
        artifacts = self.artifacts(context, media_log_id)
        row = self.media_db.get_log(int(media_log_id))
        paths = self._paths(row or {})
        for metadata, path in zip(artifacts, paths):
            if metadata["id"] != artifact_id:
                continue

            def chunks(target: str = path) -> Iterator[bytes]:
                try:
                    with open(target, "rb") as handle:
                        while True:
                            chunk = handle.read(64 * 1024)
                            if not chunk:
                                break
                            yield chunk
                except FileNotFoundError as exc:
                    raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND) from exc

            return ArtifactDownload(
                filename=metadata["fileName"],
                content_type=metadata["contentType"],
                size=metadata["sizeBytes"],
                chunks=chunks(),
            )
        raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)

    def existing_paths(self, context: ManagementContext, row: dict[str, Any]) -> list[str]:
        require(context)
        # Telegram's established viewer must remain able to serve legacy cache
        # rows after an administrator changes cachePath.  Management artifact
        # endpoints above enforce the current root for every download.
        return self._paths(row, enforce_root=False)

    def fmt_bjt(self, value: Any) -> str:
        return self.media_db.fmt_bjt(value)

    def seconds_since(self, value: Any) -> int:
        return int(self.media_db.seconds_since(value))


DEFAULT_MEDIA_CONTROL = MediaControl()
