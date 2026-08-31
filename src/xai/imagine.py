"""xAI Imagine image/video relay backed by existing xAI OAuth accounts.

Image requests enter through Parrot's existing OpenAI-compatible image routes and
are selected here only when the requested model is explicitly configured as an
xAI image model.  Video requests use xAI's asynchronous native endpoints.  The
module replaces downstream authentication, but otherwise keeps media payloads
and upstream responses as close to their native contracts as possible.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from fastapi import Request
from fastapi.responses import Response

from .. import (
    apikey_limiter,
    auth,
    concurrency,
    config,
    cooldown,
    errors,
    load_balancing,
    media_db,
    network,
    scorer,
    state_db,
)
from ..channel import registry
from ..channel.xai_oauth_channel import XAIOAuthChannel


_IMAGE_MODEL_PREFIX = "grok-imagine-image"
_EXPLICIT_SAFE_FAILOVER_STATUSES = frozenset({401, 403, 429})
_MEDIA_CACHE_HARD_FILE_LIMIT = 128 * 1024 * 1024
_MEDIA_EXTENSIONS = {
    "image/gif": "gif",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/webm": "webm",
}
_HOP_BY_HOP_RESPONSE_HEADERS = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    # httpx has already decoded response content.
    "content-encoding",
    # Never hand an upstream session cookie to a downstream API-key client.
    "set-cookie",
})

# OpenAI image sizes accepted by common clients.  Explicit xAI
# aspect_ratio/resolution fields take precedence over this compatibility map.
_IMAGE_SIZE_MAP: dict[str, tuple[str, str | None]] = {
    "auto": ("auto", None),
    "1024x1024": ("1:1", "1k"),
    "2048x2048": ("1:1", "2k"),
    "1792x1024": ("16:9", "1k"),
    "1024x1792": ("9:16", "1k"),
    "1536x1024": ("3:2", "1k"),
    "1024x1536": ("2:3", "1k"),
}

# OpenAI Videos compatibility sizes.  xAI expresses orientation and resolution
# separately; the 1024/1792 and 1080/1920 variants map to xAI's 1080p tier.
_VIDEO_SIZE_MAP: dict[str, tuple[str, str]] = {
    "720x1280": ("9:16", "720p"),
    "1280x720": ("16:9", "720p"),
    "1024x1792": ("9:16", "1080p"),
    "1792x1024": ("16:9", "1080p"),
    "1080x1920": ("9:16", "1080p"),
    "1920x1080": ("16:9", "1080p"),
    "480x854": ("9:16", "480p"),
    "854x480": ("16:9", "480p"),
}


@dataclass(frozen=True)
class _PostResult:
    response: httpx.Response
    channel: XAIOAuthChannel


def _provider_cfg() -> dict[str, Any]:
    raw = config.get().get("xaiOAuth") or {}
    return raw if isinstance(raw, dict) else {}


def _configured_models(key: str) -> list[str]:
    values = _provider_cfg().get(key) or []
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        model = str(value or "").strip()
        if model and model not in out:
            out.append(model)
    return out


def image_models() -> list[str]:
    """Configured models that select xAI on the shared Images API routes."""
    return _configured_models("imageModels")


def video_models() -> list[str]:
    return _configured_models("videoModels")


def is_xai_image_model(model: str | None) -> bool:
    return bool(model and model in image_models())


def looks_like_xai_image_model(model: str | None) -> bool:
    return bool(model and model.lower().startswith(_IMAGE_MODEL_PREFIX))


def _model_allowed(model: str, allowed_models: list[str]) -> bool:
    return not allowed_models or model in allowed_models


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _media_timeout() -> httpx.Timeout:
    cfg = _provider_cfg()
    total = _positive_float(cfg.get("mediaRequestTimeoutSeconds"), 180.0)
    connect = _positive_float((config.get().get("timeouts") or {}).get("connect"), 10.0)
    return httpx.Timeout(total, connect=connect)


def _video_job_ttl_seconds() -> int:
    return _positive_int(_provider_cfg().get("videoJobTtlSeconds"), 10800)


def _media_url(channel: XAIOAuthChannel, path: str) -> str:
    parsed = urlsplit(str(channel.base_url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("xAI OAuth media base URL must be an absolute HTTP(S) URL")
    base_path = parsed.path.rstrip("/")
    if base_path.endswith("/responses"):
        base_path = base_path[: -len("/responses")]
    joined_path = f"{base_path}/{path.lstrip('/')}"
    return urlunsplit((parsed.scheme, parsed.netloc, joined_path, "", ""))


def _safe_response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in _HOP_BY_HOP_RESPONSE_HEADERS
    }


def _downstream_response(response: httpx.Response) -> Response:
    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=_safe_response_headers(response.headers),
    )


def _response_object(response: httpx.Response | Response) -> dict[str, Any]:
    try:
        if isinstance(response, httpx.Response):
            value = response.json()
        else:
            value = json.loads(bytes(response.body or b"{}"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _response_usage(obj: dict[str, Any]) -> dict[str, Any] | None:
    usage = obj.get("usage")
    return dict(usage) if isinstance(usage, dict) else None


def _response_error(response: httpx.Response | Response) -> tuple[str, str]:
    obj = _response_object(response)
    error = obj.get("error")
    if isinstance(error, dict):
        error_type = str(error.get("type") or error.get("code") or "upstream_error")
        message = str(error.get("message") or error_type)
        return error_type, message
    if isinstance(error, str) and error:
        return "upstream_error", error
    message = str(obj.get("message") or "").strip()
    return "upstream_error", message or f"xAI Imagine HTTP {response.status_code}"


def _float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _video_seconds(payload: dict[str, Any], response_obj: dict[str, Any] | None = None) -> float | None:
    response_obj = response_obj or {}
    video = response_obj.get("video")
    if isinstance(video, dict):
        value = _float_or_none(video.get("duration"))
        if value is not None:
            return value
    for key in ("duration", "seconds"):
        value = _float_or_none(payload.get(key))
        if value is not None:
            return value
    return None


def _video_log_status(upstream_status: str | None) -> str | None:
    value = str(upstream_status or "").strip().lower()
    if value in {"done", "completed", "succeeded", "success"}:
        return "success"
    if value in {"failed", "error"}:
        return "failed"
    if value in {"expired", "cancelled", "canceled"}:
        return "expired" if value == "expired" else "cancelled"
    if value in {"pending", "queued", "processing", "in_progress", "running"}:
        return "pending"
    return None


def _media_cache_settings() -> dict[str, Any]:
    # Keep one cache policy for GPT images and Grok images/videos.  The import is
    # local to avoid coupling module initialization to the OpenAI image routes.
    from ..openai.images_simple import settings

    return settings()


def _media_cache_file_limit(cfg: dict[str, Any]) -> int:
    try:
        aggregate_limit = int(cfg.get("cacheMaxBytes") or 0)
    except (TypeError, ValueError):
        aggregate_limit = 0
    if aggregate_limit > 0:
        return min(aggregate_limit, _MEDIA_CACHE_HARD_FILE_LIMIT)
    return _MEDIA_CACHE_HARD_FILE_LIMIT


def _decode_media_data_url(value: str, *, max_bytes: int) -> tuple[bytes, str]:
    header, sep, encoded = str(value or "").partition(",")
    if not sep or not header.lower().startswith("data:") or ";base64" not in header.lower():
        raise ValueError("unsupported generated media data URL")
    if len(encoded) * 3 // 4 > max_bytes:
        raise ValueError("generated media exceeds cache file limit")
    raw = base64.b64decode(encoded, validate=False)
    if len(raw) > max_bytes:
        raise ValueError("generated media exceeds cache file limit")
    mime = header[5:].split(";", 1)[0].strip().lower()
    return raw, mime


def _is_allowed_xai_media_url(value: str) -> bool:
    parsed = urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme.lower() == "https" and (host == "x.ai" or host.endswith(".x.ai"))


async def _download_xai_media(
    value: str,
    *,
    channel: XAIOAuthChannel,
    model: str,
    max_bytes: int,
) -> tuple[bytes, str]:
    if str(value or "").startswith("data:"):
        return _decode_media_data_url(value, max_bytes=max_bytes)
    if not _is_allowed_xai_media_url(value):
        raise ValueError("generated media URL is outside x.ai")

    timeout = httpx.Timeout(90.0, connect=10.0)
    async with network.async_client(
        timeout=timeout,
        proxy_purpose="oauth_xai",
        proxy_channel=channel.key,
        proxy_model=model,
        follow_redirects=False,
    ) as client:
        async with client.stream(
            "GET",
            value,
            headers={"Accept": "image/*, video/*, application/octet-stream"},
        ) as response:
            if response.status_code != 200:
                raise ValueError(f"generated media download returned HTTP {response.status_code}")
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > max_bytes:
                        raise ValueError("generated media exceeds cache file limit")
                except ValueError as exc:
                    if "exceeds" in str(exc):
                        raise
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("generated media exceeds cache file limit")
                chunks.append(chunk)
            mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            return b"".join(chunks), mime


def _cached_media_extension(*, media_type: str, mime: str, source_url: str) -> str:
    normalized_mime = str(mime or "").lower()
    if normalized_mime in _MEDIA_EXTENSIONS:
        return _MEDIA_EXTENSIONS[normalized_mime]
    suffix = Path(urlsplit(source_url).path).suffix.lower().lstrip(".")
    allowed = {"gif", "jpeg", "jpg", "m4v", "mov", "mp4", "png", "webm", "webp"}
    if suffix in allowed:
        return "jpg" if suffix == "jpeg" else suffix
    return "mp4" if media_type == "video" else "jpg"


def _write_cached_media(
    raw: bytes,
    *,
    cfg: dict[str, Any],
    media_type: str,
    action: str,
    extension: str,
    index: int,
) -> str:
    from ..openai import images_simple

    root = images_simple._cache_root(cfg)
    day = time.strftime("%Y%m%d", time.localtime())
    out_dir = root / day
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = (
        f"xai-{media_type}-{action}-{int(time.time())}-"
        f"{uuid.uuid4().hex[:10]}-{index}.{extension}"
    )
    path = out_dir / filename
    temporary = out_dir / f".{filename}.tmp"
    temporary.write_bytes(raw)
    os.replace(temporary, path)
    return str(path)


async def _cache_xai_results(
    items: list[dict[str, Any]],
    *,
    media_type: str,
    action: str,
    channel: XAIOAuthChannel,
    model: str,
) -> tuple[list[str], int]:
    cfg = _media_cache_settings()
    if not cfg.get("cacheEnabled") or not items:
        return [], 0
    max_bytes = _media_cache_file_limit(cfg)
    paths: list[str] = []
    total_bytes = 0
    for index, item in enumerate(items):
        try:
            source_url = str(item.get("url") or "")
            b64_value = item.get("b64_json")
            if isinstance(b64_value, str) and b64_value:
                if len(b64_value) * 3 // 4 > max_bytes:
                    raise ValueError("generated media exceeds cache file limit")
                raw = base64.b64decode(b64_value, validate=False)
                mime = str(item.get("mime_type") or "")
            elif source_url:
                raw, downloaded_mime = await _download_xai_media(
                    source_url,
                    channel=channel,
                    model=model,
                    max_bytes=max_bytes,
                )
                mime = str(item.get("mime_type") or downloaded_mime)
            else:
                continue
            if not raw or len(raw) > max_bytes:
                raise ValueError("generated media is empty or exceeds cache file limit")
            extension = _cached_media_extension(
                media_type=media_type,
                mime=mime,
                source_url=source_url,
            )
            path = await asyncio.to_thread(
                _write_cached_media,
                raw,
                cfg=cfg,
                media_type=media_type,
                action=action,
                extension=extension,
                index=index,
            )
            paths.append(path)
            total_bytes += len(raw)
        except Exception as exc:
            print(
                f"[xai-imagine] {media_type} cache failed "
                f"index={index} type={type(exc).__name__}"
            )

    if paths:
        try:
            from ..openai import images_simple

            root = images_simple._cache_root(cfg)
            await asyncio.to_thread(images_simple._cleanup_cache, root, cfg)
        except Exception as exc:
            print(f"[xai-imagine] media cache cleanup failed type={type(exc).__name__}")
    return paths, total_bytes


def _existing_cache_paths(row: dict[str, Any] | None) -> list[str]:
    if not row:
        return []
    try:
        values = json.loads(row.get("cache_paths") or "[]")
    except Exception:
        return []
    if not isinstance(values, list):
        return []
    return [path for path in values if isinstance(path, str) and os.path.exists(path)]


async def _start_media_log(**fields: Any) -> int | None:
    try:
        return await asyncio.to_thread(media_db.start_call, **fields)
    except Exception as exc:
        print(f"[xai-imagine] media log start failed type={type(exc).__name__}")
        return None


async def _finish_media_log(log_id: int | None, **fields: Any) -> None:
    if log_id is None:
        return
    try:
        await asyncio.to_thread(media_db.finish_call, log_id, **fields)
    except Exception as exc:
        print(f"[xai-imagine] media log update failed type={type(exc).__name__}")


async def _update_video_log(request_id: str, **fields: Any) -> None:
    try:
        await asyncio.to_thread(media_db.update_job, request_id, **fields)
    except Exception as exc:
        print(f"[xai-imagine] video log update failed type={type(exc).__name__}")


def _bad_request(message: str, *, param: str | None = None) -> Response:
    return errors.json_error_openai(
        400,
        errors.ErrTypeOpenAI.INVALID_REQUEST,
        message,
        param=param,
    )


def _permission_error(message: str) -> Response:
    return errors.json_error_openai(
        403,
        errors.ErrTypeOpenAI.PERMISSION,
        message,
    )


def _eligible_channels(kind: str, model: str) -> list[XAIOAuthChannel]:
    candidates: list[tuple[XAIOAuthChannel, str]] = []
    for channel in registry.all_channels():
        if not isinstance(channel, XAIOAuthChannel):
            continue
        if not channel.enabled or channel.disabled_reason:
            continue
        if not channel.supports_media_model(kind, model):
            continue
        if cooldown.is_blocked(channel.key, model):
            continue
        candidates.append((channel, model))

    selection = str(config.get().get("channelSelection") or "smart").lower()
    if selection == "smart":
        candidates = scorer.sort_by_score(candidates)
    elif selection == "priority":
        candidates = load_balancing.sort_candidates_by_priority(
            candidates, config.get(), requested_model=model,
        )
    return [channel for channel, _resolved in candidates]


async def _request_upstream(
    channel: XAIOAuthChannel,
    *,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes | None,
    model: str,
) -> httpx.Response:
    async with network.async_client(
        timeout=_media_timeout(),
        proxy_purpose="oauth_xai",
        proxy_channel=channel.key,
        proxy_model=model,
        follow_redirects=False,
    ) as client:
        return await client.request(
            method,
            _media_url(channel, path),
            headers=headers,
            content=body,
        )


def _record_upstream_status(channel: XAIOAuthChannel, model: str, status: int) -> None:
    if 200 <= status < 300:
        cooldown.clear_on_success(channel.key, model)
    elif status in _EXPLICIT_SAFE_FAILOVER_STATUSES or status >= 500:
        cooldown.record_error(channel.key, model, f"xAI Imagine HTTP {status}")


async def _post_with_safe_failover(
    *,
    kind: str,
    model: str,
    path: str,
    payload: dict[str, Any],
) -> _PostResult | Response:
    """POST once per explicitly rejecting account; never retry ambiguous I/O."""
    candidates = _eligible_channels(kind, model)
    if not candidates:
        return errors.json_error_openai(
            503,
            errors.ErrTypeOpenAI.SERVER,
            f"no available xAI OAuth account for {kind} model {model}",
        )

    last_rejection: _PostResult | None = None
    acquired_any = False
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    for channel in candidates:
        if not await concurrency.try_acquire(channel.key):
            continue
        acquired_any = True
        try:
            try:
                headers = await channel.build_media_headers()
            except Exception as exc:
                print(
                    f"[xai-imagine] OAuth headers failed channel={channel.key} "
                    f"type={type(exc).__name__}"
                )
                cooldown.record_error(channel.key, model, "xAI Imagine OAuth headers failed")
                continue

            try:
                response = await _request_upstream(
                    channel,
                    method="POST",
                    path=path,
                    headers=headers,
                    body=body,
                    model=model,
                )
            except httpx.TimeoutException:
                # The server may already have accepted and billed the POST.  Do not
                # retry another account when acceptance is ambiguous.
                return errors.json_error_openai(
                    504,
                    errors.ErrTypeOpenAI.TIMEOUT,
                    "xAI Imagine request timed out; it was not retried",
                )
            except Exception as exc:
                print(
                    f"[xai-imagine] upstream request failed channel={channel.key} "
                    f"type={type(exc).__name__}"
                )
                return errors.json_error_openai(
                    502,
                    errors.ErrTypeOpenAI.SERVER,
                    "xAI Imagine upstream request failed; it was not retried",
                )

            _record_upstream_status(channel, model, response.status_code)
            result = _PostResult(response=response, channel=channel)
            if response.status_code in _EXPLICIT_SAFE_FAILOVER_STATUSES:
                last_rejection = result
                continue
            return result
        finally:
            concurrency.release(channel.key)

    if last_rejection is not None:
        return last_rejection
    message = (
        "all xAI OAuth accounts are at capacity"
        if not acquired_any
        else "no xAI OAuth account could establish media authentication"
    )
    return errors.json_error_openai(
        503,
        errors.ErrTypeOpenAI.SERVER,
        message,
    )


def _apply_image_size(payload: dict[str, Any], size: str | None) -> None:
    if not size:
        return
    need_aspect = not payload.get("aspect_ratio")
    need_resolution = not payload.get("resolution")
    if not need_aspect and not need_resolution:
        return
    mapped = _IMAGE_SIZE_MAP.get(size.strip().lower())
    if mapped is None:
        raise ValueError(
            f"unsupported size {size!r} for xAI image model; use aspect_ratio/resolution"
        )
    aspect_ratio, resolution = mapped
    if need_aspect:
        payload["aspect_ratio"] = aspect_ratio
    if need_resolution and resolution:
        payload["resolution"] = resolution


def _build_image_payload(parsed: Any, *, action: str) -> dict[str, Any]:
    model = str(parsed.model or "").strip()
    payload: dict[str, Any] = {
        "model": model,
        "prompt": parsed.prompt,
        "n": parsed.requested_n,
    }
    if parsed.requested_n > 10:
        raise ValueError("n must be between 1 and 10 for xAI image models")
    if getattr(parsed, "response_format_explicit", False):
        payload["response_format"] = parsed.response_format

    xai_options = getattr(parsed, "xai_options", {}) or {}
    for key in ("aspect_ratio", "resolution", "user", "storage_options"):
        if key in xai_options:
            payload[key] = xai_options[key]
    quality = (getattr(parsed, "native_options", {}) or {}).get("quality")
    if quality:
        payload["quality"] = quality
    _apply_image_size(payload, parsed.size)

    if action == "edit":
        images = list(parsed.input_images or [])
        if len(images) > 3:
            raise ValueError("xAI image edits support at most 3 input images")
        refs = [{"url": image} for image in images]
        if len(refs) == 1:
            payload["image"] = refs[0]
        else:
            payload["images"] = refs
        if parsed.mask_url:
            payload["mask"] = {"url": parsed.mask_url}
    return payload


async def handle_image(
    parsed: Any,
    *,
    action: str,
    key_name: str,
    allowed_models: list[str],
) -> Response:
    """Relay and log one model-selected request from the shared Images routes."""
    model = str(parsed.model or "").strip()
    if not _model_allowed(model, allowed_models):
        return _permission_error("model is not allowed for this API key")
    try:
        payload = _build_image_payload(parsed, action=action)
    except ValueError as exc:
        return _bad_request(str(exc))

    log_id = await _start_media_log(
        request_id=str(uuid.uuid4()),
        api_key_name=key_name,
        provider="xai",
        media_type="image",
        action=action,
        model=model,
        prompt=parsed.prompt,
        size=parsed.size,
        aspect_ratio=str(payload.get("aspect_ratio") or "") or None,
        resolution=str(payload.get("resolution") or "") or None,
        requested_count=parsed.requested_n,
    )
    path = "/images/generations" if action == "generate" else "/images/edits"
    started = time.monotonic()
    result = await _post_with_safe_failover(
        kind="image",
        model=model,
        path=path,
        payload=payload,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if isinstance(result, Response):
        error_type, message = _response_error(result)
        await _finish_media_log(
            log_id,
            status="failed",
            duration_ms=elapsed_ms,
            request_duration_ms=elapsed_ms,
            error_type=error_type,
            error_message=message,
            http_status=result.status_code,
        )
        return result

    response = result.response
    obj = _response_object(response)
    usage = _response_usage(obj)
    data = obj.get("data")
    image_items = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
    image_count = len(data) if isinstance(data, list) else 0
    success = 200 <= response.status_code < 300
    cache_paths: list[str] = []
    cached_bytes = 0
    if success and image_items:
        cache_paths, cached_bytes = await _cache_xai_results(
            image_items,
            media_type="image",
            action=action,
            channel=result.channel,
            model=model,
        )
    error_type: str | None = None
    error_message: str | None = None
    if not success:
        error_type, error_message = _response_error(response)
    await _finish_media_log(
        log_id,
        status="success" if success else "failed",
        account_key=result.channel.account_key,
        account_email=result.channel.email,
        model=str(obj.get("model") or model),
        upstream_status=str(obj.get("status") or "") or None,
        duration_ms=elapsed_ms,
        request_duration_ms=elapsed_ms,
        image_count=image_count,
        usage=usage,
        cached_media_count=len(cache_paths),
        media_bytes=cached_bytes or None,
        cache_paths=cache_paths,
        error_type=error_type,
        error_message=error_message,
        http_status=response.status_code,
    )
    return _downstream_response(response)


def _normalize_reference(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    out = dict(value)
    if "url" not in out and isinstance(out.get("image_url"), str):
        out["url"] = out.pop("image_url")
    return out


def _normalize_video_payload(body: dict[str, Any], *, action: str) -> dict[str, Any]:
    payload = dict(body)
    if action == "generate":
        if "image" not in payload and "input_reference" in payload:
            payload["image"] = _normalize_reference(payload.pop("input_reference"))
        elif "image" in payload:
            payload["image"] = _normalize_reference(payload["image"])

        if isinstance(payload.get("reference_images"), list):
            payload["reference_images"] = [
                _normalize_reference(item) for item in payload["reference_images"]
            ]

        raw_size = payload.pop("size", None)
        if raw_size is not None:
            need_aspect = not payload.get("aspect_ratio")
            need_resolution = not payload.get("resolution")
            if need_aspect or need_resolution:
                size = str(raw_size).strip().lower()
                mapped = _VIDEO_SIZE_MAP.get(size)
                if mapped is None:
                    raise ValueError(
                        f"unsupported size {raw_size!r} for xAI video model; "
                        "use aspect_ratio/resolution"
                    )
                aspect_ratio, resolution = mapped
                if need_aspect:
                    payload["aspect_ratio"] = aspect_ratio
                if need_resolution:
                    payload["resolution"] = resolution
    elif "video" in payload:
        payload["video"] = _normalize_reference(payload["video"])
    return payload


async def _read_json_object(request: Request) -> dict[str, Any] | Response:
    try:
        body = await request.json()
    except (
        apikey_limiter.RequestBodyTooLarge,
        apikey_limiter.QueuedBodySpoolError,
    ):
        raise
    except Exception as exc:
        return _bad_request(f"invalid json: {exc}")
    if not isinstance(body, dict):
        return _bad_request("request body must be a JSON object")
    return body


async def handle_video_create(request: Request, *, action: str) -> Response:
    """Create one xAI asynchronous generation/edit/extension task."""
    key_name, allowed_models, auth_error = auth.validate(request.headers)
    if auth_error:
        return errors.json_error_openai(401, errors.ErrTypeOpenAI.AUTH, auth_error)
    assert key_name is not None
    if not auth.videos_allowed(key_name):
        return _permission_error("this API key is not allowed to use video endpoints")

    body = await _read_json_object(request)
    if isinstance(body, Response):
        return body

    model = str(body.get("model") or "").strip()
    configured = video_models()
    if not model and configured:
        model = configured[0]
        body["model"] = model
    if not model:
        return _bad_request("model is required", param="model")
    if model not in configured:
        return _bad_request(f"unsupported xAI video model {model!r}", param="model")
    if not _model_allowed(model, allowed_models):
        return _permission_error("model is not allowed for this API key")

    try:
        payload = _normalize_video_payload(body, action=action)
    except ValueError as exc:
        return _bad_request(str(exc), param="size")

    paths = {
        "generate": "/videos/generations",
        "edit": "/videos/edits",
        "extend": "/videos/extensions",
    }
    path = paths.get(action)
    if path is None:
        raise ValueError(f"unsupported video action: {action}")

    log_id = await _start_media_log(
        request_id=str(uuid.uuid4()),
        api_key_name=key_name,
        provider="xai",
        media_type="video",
        action=action,
        model=model,
        prompt=str(payload.get("prompt") or ""),
        size=str(body.get("size") or "") or None,
        aspect_ratio=str(payload.get("aspect_ratio") or "") or None,
        resolution=str(payload.get("resolution") or "") or None,
        media_duration_seconds=_video_seconds(payload),
    )
    started = time.monotonic()
    result = await _post_with_safe_failover(
        kind="video",
        model=model,
        path=path,
        payload=payload,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if isinstance(result, Response):
        error_type, message = _response_error(result)
        await _finish_media_log(
            log_id,
            status="failed",
            duration_ms=elapsed_ms,
            request_duration_ms=elapsed_ms,
            error_type=error_type,
            error_message=message,
            http_status=result.status_code,
        )
        return result

    response = result.response
    response_body = _response_object(response)
    upstream_request_id = str(response_body.get("request_id") or "").strip()
    upstream_status = str(response_body.get("status") or "").strip()
    success = 200 <= response.status_code < 300
    local_status = "failed"
    expires_at: float | None = None
    error_type: str | None = None
    error_message: str | None = None

    if success and upstream_request_id:
        local_status = _video_log_status(upstream_status) or "pending"
        ttl_seconds = _video_job_ttl_seconds()
        expires_at = time.time() + ttl_seconds
        try:
            state_db.xai_video_job_save(
                upstream_request_id,
                channel_key=result.channel.key,
                api_key_name=key_name,
                model=model,
                ttl_seconds=ttl_seconds,
            )
        except Exception as exc:
            # Upstream may already have accepted and billed the request.  Keep its
            # response and audit the binding failure instead of retrying the POST.
            print(
                f"[xai-imagine] video binding save failed "
                f"type={type(exc).__name__}"
            )
            error_type = "binding_save_failed"
            error_message = "video task was accepted but its local identity binding could not be saved"
    elif success:
        error_type = "invalid_upstream_response"
        error_message = "xAI video creation response did not include request_id"
    else:
        error_type, error_message = _response_error(response)

    video_cache_paths: list[str] = []
    video_cached_bytes = 0
    video_obj = response_body.get("video")
    if local_status == "success" and isinstance(video_obj, dict):
        video_cache_paths, video_cached_bytes = await _cache_xai_results(
            [video_obj],
            media_type="video",
            action=action,
            channel=result.channel,
            model=model,
        )

    await _finish_media_log(
        log_id,
        status=local_status,
        account_key=result.channel.account_key,
        account_email=result.channel.email,
        model=str(response_body.get("model") or model),
        upstream_request_id=upstream_request_id or None,
        upstream_status=upstream_status or ("pending" if local_status == "pending" else None),
        progress=_float_or_none(response_body.get("progress")),
        duration_ms=elapsed_ms if local_status in {"success", "failed", "expired", "cancelled"} else None,
        request_duration_ms=elapsed_ms,
        media_duration_seconds=_video_seconds(payload, response_body),
        usage=_response_usage(response_body),
        cached_media_count=len(video_cache_paths),
        media_bytes=video_cached_bytes or None,
        cache_paths=video_cache_paths,
        error_type=error_type,
        error_message=error_message,
        http_status=response.status_code,
        expires_at=expires_at,
    )
    return _downstream_response(response)


async def handle_video_result(request: Request, request_id: str) -> Response:
    """Poll one video task with the same API key and OAuth account that created it."""
    key_name, _allowed_models, auth_error = auth.validate(request.headers)
    if auth_error:
        return errors.json_error_openai(401, errors.ErrTypeOpenAI.AUTH, auth_error)
    assert key_name is not None
    if not auth.videos_allowed(key_name):
        return _permission_error("this API key is not allowed to use video endpoints")

    normalized_id = str(request_id or "").strip()
    if not normalized_id or len(normalized_id) > 256:
        return _bad_request("invalid video request_id", param="request_id")
    binding = state_db.xai_video_job_load(normalized_id)
    # Deliberately use the same 404 for missing and cross-key IDs.
    if binding is None or binding.get("api_key_name") != key_name:
        return errors.json_error_openai(
            404,
            errors.ErrTypeOpenAI.INVALID_REQUEST,
            "unknown or expired video request_id",
            param="request_id",
        )

    channel = registry.get_channel(str(binding.get("channel_key") or ""))
    if not isinstance(channel, XAIOAuthChannel) or not channel.enabled or channel.disabled_reason:
        await _update_video_log(
            normalized_id,
            status=None,
            last_polled_at=time.time(),
            error_type="bound_account_unavailable",
            error_message="the xAI OAuth account for this video task is unavailable",
            http_status=503,
        )
        return errors.json_error_openai(
            503,
            errors.ErrTypeOpenAI.SERVER,
            "the xAI OAuth account for this video task is unavailable",
        )
    model = str(binding.get("model") or "")
    if not await concurrency.try_acquire(channel.key):
        await _update_video_log(
            normalized_id,
            status=None,
            last_polled_at=time.time(),
            error_type="bound_account_at_capacity",
            error_message="the xAI OAuth account for this video task is at capacity",
            http_status=503,
        )
        return errors.json_error_openai(
            503,
            errors.ErrTypeOpenAI.SERVER,
            "the xAI OAuth account for this video task is at capacity",
        )

    try:
        try:
            headers = await channel.build_media_headers()
            response = await _request_upstream(
                channel,
                method="GET",
                path=f"/videos/{quote(normalized_id, safe='')}",
                headers=headers,
                body=None,
                model=model,
            )
        except httpx.TimeoutException:
            await _update_video_log(
                normalized_id,
                status=None,
                last_polled_at=time.time(),
                error_type="poll_timeout",
                error_message="xAI video status request timed out",
                http_status=504,
            )
            return errors.json_error_openai(
                504,
                errors.ErrTypeOpenAI.TIMEOUT,
                "xAI video status request timed out",
            )
        except Exception as exc:
            print(
                f"[xai-imagine] video status request failed channel={channel.key} "
                f"type={type(exc).__name__}"
            )
            await _update_video_log(
                normalized_id,
                status=None,
                last_polled_at=time.time(),
                error_type="poll_upstream_error",
                error_message="xAI video status request failed",
                http_status=502,
            )
            return errors.json_error_openai(
                502,
                errors.ErrTypeOpenAI.SERVER,
                "xAI video status request failed",
            )

        _record_upstream_status(channel, model, response.status_code)
        response_body = _response_object(response)
        upstream_status = str(response_body.get("status") or "").strip()
        if 200 <= response.status_code < 300:
            local_status = _video_log_status(upstream_status)
            error_type: str | None = None
            error_message: str | None = None
            if local_status in {"failed", "expired", "cancelled"}:
                error_type, error_message = _response_error(response)

            cache_fields: dict[str, Any] = {}
            existing_log = await asyncio.to_thread(
                media_db.get_by_upstream_request_id,
                normalized_id,
            )
            if local_status == "success" and not _existing_cache_paths(existing_log):
                video_obj = response_body.get("video")
                if isinstance(video_obj, dict):
                    cache_paths, cached_bytes = await _cache_xai_results(
                        [video_obj],
                        media_type="video",
                        action=str((existing_log or {}).get("action") or "generate"),
                        channel=channel,
                        model=model,
                    )
                    if cache_paths:
                        cache_fields = {
                            "cached_media_count": len(cache_paths),
                            "media_bytes": cached_bytes,
                            "cache_paths": cache_paths,
                        }
            await _update_video_log(
                normalized_id,
                status=local_status,
                model=str(response_body.get("model") or model),
                upstream_status=upstream_status or None,
                progress=_float_or_none(response_body.get("progress")),
                media_duration_seconds=_video_seconds({}, response_body),
                usage=_response_usage(response_body),
                error_type=error_type,
                error_message=error_message,
                http_status=response.status_code,
                last_polled_at=time.time(),
                **cache_fields,
            )
        else:
            error_type, error_message = _response_error(response)
            await _update_video_log(
                normalized_id,
                status=None,
                upstream_status=upstream_status or None,
                progress=_float_or_none(response_body.get("progress")),
                usage=_response_usage(response_body),
                error_type=error_type,
                error_message=error_message,
                http_status=response.status_code,
                last_polled_at=time.time(),
            )
        return _downstream_response(response)
    finally:
        concurrency.release(channel.key)
