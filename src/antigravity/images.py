"""Antigravity implementation for Parrot's shared OpenAI Images routes."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import httpx
from fastapi.responses import JSONResponse, Response

from .. import (
    channel_state,
    concurrency,
    config,
    cooldown,
    errors,
    load_balancing,
    media_cache,
    media_db,
    network,
    scorer,
)
from ..async_owned import await_owned
from ..channel import registry
from ..channel.antigravity_oauth_channel import AntigravityOAuthChannel
from ..oauth import antigravity as ag_provider
from ..providers.antigravity_codec import unwrap_cloud_code, wrap_cloud_code
from ..providers.antigravity_errors import parse_antigravity_429
from ..protocols.runtime import transient_retry_allowed, transient_retry_limit

_MAX_RESPONSE_BYTES = 110 * 1024 * 1024
_SIZE_MAP = {
    "auto": None,
    "1024x1024": "1:1",
    "1536x1024": "3:2",
    "1024x1536": "2:3",
}


def is_antigravity_image_model(model: str | None) -> bool:
    name = str(model or "").strip()
    return bool(name) and any(
        isinstance(ch, AntigravityOAuthChannel) and ch.supports_media_model("image", name)
        for ch in registry.all_channels()
    )


def looks_like_antigravity_image_model(model: str | None) -> bool:
    name = str(model or "").lower()
    return bool(name) and "gemini" in name and "image" in name


def _bad(message: str, param: str | None = None) -> Response:
    return errors.json_error_openai(400, errors.ErrTypeOpenAI.INVALID_REQUEST, message, param=param)


def _eligible(model: str) -> list[AntigravityOAuthChannel]:
    pairs: list[tuple[AntigravityOAuthChannel, str]] = []
    for ch in registry.all_channels():
        if not isinstance(ch, AntigravityOAuthChannel):
            continue
        if not ch.enabled or ch.disabled_reason or not ch.supports_media_model("image", model):
            continue
        if cooldown.is_blocked(channel_state.effect_key(ch), model):
            continue
        pairs.append((ch, model))
    selection = str(config.get().get("channelSelection") or "smart").lower()
    if selection == "smart": pairs = scorer.sort_by_score(pairs)
    elif selection == "priority": pairs = load_balancing.sort_candidates_by_priority(pairs, config.get(), requested_model=model)
    return [p[0] for p in pairs]


def _build_request(parsed: Any) -> dict[str, Any]:
    if parsed.requested_n > 4:
        raise ValueError("n must be between 1 and 4 for Antigravity image models")
    unsupported = []
    opts = parsed.native_options or {}
    for field in ("style", "background", "moderation", "input_fidelity", "output_compression", "partial_images"):
        if field in opts:
            unsupported.append(field)
    if unsupported:
        raise ValueError(f"unsupported parameter(s) for Antigravity image models: {', '.join(unsupported)}")
    quality = str(opts.get("quality") or "auto").lower()
    if quality not in {"auto", "standard", "hd", "high"}:
        raise ValueError("quality must be auto, standard, hd, or high for Antigravity image models")
    if "output_format" in opts:
        raise ValueError("output_format is not supported by Antigravity; the upstream image MIME is returned as generated")
    size = str(parsed.size or "auto").lower()
    if size not in _SIZE_MAP:
        raise ValueError(f"unsupported size {parsed.size!r} for Antigravity; use auto, 1024x1024, 1536x1024, or 1024x1536")
    image_cfg: dict[str, Any] = {}
    aspect = _SIZE_MAP[size]
    if aspect: image_cfg["aspectRatio"] = aspect
    if quality in {"hd", "high"}: image_cfg["imageSize"] = "2K"
    elif quality == "standard": image_cfg["imageSize"] = "1K"
    generation: dict[str, Any] = {"responseModalities": ["IMAGE"]}
    if parsed.requested_n > 1: generation["candidateCount"] = parsed.requested_n
    if image_cfg: generation["imageConfig"] = image_cfg
    return {"contents": [{"role": "user", "parts": [{"text": parsed.prompt}]}], "generationConfig": generation}


async def _read_bounded(response: httpx.Response) -> bytes:
    declared = response.headers.get("content-length")
    if declared:
        try:
            if int(declared) > _MAX_RESPONSE_BYTES: raise ValueError("Antigravity image response is too large")
        except ValueError as exc:
            raise ValueError("invalid or oversized Antigravity Content-Length") from exc
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES: raise ValueError("Antigravity image response is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_with_cache_items(
    obj: dict[str, Any], *, model: str, response_format: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    data = unwrap_cloud_code(obj)
    candidates = data.get("candidates") if isinstance(data, dict) else None
    out: list[dict[str, Any]] = []
    cache_items: list[dict[str, str]] = []
    for candidate in candidates or []:
        parts = ((candidate.get("content") or {}).get("parts") or []) if isinstance(candidate, dict) else []
        for part in parts:
            inline = part.get("inlineData") or part.get("inline_data") if isinstance(part, dict) else None
            if not isinstance(inline, dict): continue
            encoded = str(inline.get("data") or "")
            mime = str(inline.get("mimeType") or inline.get("mime_type") or "image/png")
            if not encoded: continue
            item = {"b64_json": encoded} if response_format == "b64_json" else {"url": f"data:{mime};base64,{encoded}"}
            out.append(item)
            cache_items.append({"base64": encoded, "mime": mime})
    if not out: raise ValueError("Antigravity returned no decodable image")
    payload: dict[str, Any] = {"created": int(time.time()), "data": out, "model": model}
    usage = data.get("usageMetadata") or data.get("usage_metadata") if isinstance(data, dict) else None
    if usage: payload["usage"] = usage
    return payload, cache_items


def _decode(obj: dict[str, Any], *, model: str, response_format: str) -> dict[str, Any]:
    """Backward-compatible payload-only decoder used by existing tests."""
    payload, _ = _decode_with_cache_items(
        obj, model=model, response_format=response_format,
    )
    return payload


def _media_cache_settings() -> dict[str, Any]:
    from ..openai.images_simple import settings

    return settings()


async def _start_log(**fields: Any) -> int | None:
    try:
        return await asyncio.to_thread(media_db.start_call, **fields)
    except Exception as exc:
        print(f"[antigravity-images] media log start failed type={type(exc).__name__}")
        return None


async def _finish_log(log_id: int | None, **fields: Any) -> None:
    if log_id is None:
        return
    try:
        await await_owned(asyncio.to_thread(media_db.finish_call, log_id, **fields))
    except Exception as exc:
        print(f"[antigravity-images] media log update failed type={type(exc).__name__}")


async def handle_image(parsed: Any, *, action: str, key_name: str, allowed_models: list[str]) -> Response:
    model = str(parsed.model or "").strip()
    request_id = str(uuid.uuid4())
    started = time.time()
    log_id: int | None = None
    current_account: AntigravityOAuthChannel | None = None
    cache_task: asyncio.Task[media_cache.CacheResult] | None = None
    log_task = asyncio.create_task(_start_log(
        request_id=request_id,
        api_key_name=key_name,
        provider="antigravity",
        media_type="image",
        action=action,
        model=model,
        prompt=str(getattr(parsed, "prompt", "") or ""),
        size=str(getattr(parsed, "size", "") or "") or None,
        requested_count=max(1, int(getattr(parsed, "requested_n", 1) or 1)),
    ))

    try:
        log_id = await await_owned(log_task)

        async def failed(
            response: Response,
            *,
            error_type: str,
            account: AntigravityOAuthChannel | None = None,
        ) -> Response:
            await _finish_log(
                log_id,
                status="failed",
                account_key=account.account_key if account else None,
                account_email=account.email if account else None,
                duration_ms=int((time.time() - started) * 1000),
                error_type=error_type,
                error_message=error_type,
                http_status=response.status_code,
                cache_status="disabled",
            )
            return response

        if action != "generate":
            return await failed(
                _bad("Antigravity image models do not support /images/edits", "model"),
                error_type="unsupported_action",
            )
        if allowed_models and model not in allowed_models:
            return await failed(
                errors.json_error_openai(
                    403, errors.ErrTypeOpenAI.PERMISSION,
                    "model is not allowed for this API key",
                ),
                error_type="model_not_allowed",
            )
        try:
            gemini = _build_request(parsed)
        except ValueError as exc:
            return await failed(_bad(str(exc)), error_type="invalid_request")
        channels = _eligible(model)
        if not channels:
            return await failed(
                errors.json_error_openai(
                    503, errors.ErrTypeOpenAI.SERVER,
                    f"no available Antigravity OAuth account for image model {model}",
                ),
                error_type="no_available_account",
            )
        last: Response | None = None
        last_account: AntigravityOAuthChannel | None = None
        retry_cfg = config.get()
        short_retry_limit = (
            transient_retry_limit(retry_cfg)
            if transient_retry_allowed("antigravityRateLimit", retry_cfg)
            else 0
        )
        for ch in channels:
            key = channel_state.effect_key(ch)
            if not await concurrency.try_acquire(key):
                continue
            current_account = ch
            try:
                manager = __import__("src.oauth_manager", fromlist=["ensure_valid_token"])
                token = await manager.ensure_valid_token(ch.account_key)
                envelope = wrap_cloud_code(
                    gemini, model=model, project_id=ch.project_id,
                    stream=False, session_id="",
                )
                headers = ch._build_headers(token, stream=False)
                url = f"{ch.base_url}/{ag_provider.API_VERSION}:generateContent"
                short_retries = 0
                google_429: dict[str, Any] = {}
                while True:
                    try:
                        async with network.async_client(
                            timeout=httpx.Timeout(180.0, connect=15.0),
                            proxy_purpose="oauth_antigravity",
                            proxy_channel=ch.key,
                            proxy_model=model,
                            follow_redirects=False,
                        ) as client:
                            async with client.stream(
                                "POST", url, headers=headers,
                                content=json.dumps(envelope, separators=(",", ":")).encode(),
                            ) as response:
                                raw = await _read_bounded(response)
                                status = response.status_code
                    except httpx.TimeoutException:
                        return await failed(
                            errors.json_error_openai(
                                504, errors.ErrTypeOpenAI.TIMEOUT,
                                "Antigravity image generation timed out; it was not retried",
                            ),
                            error_type="timeout",
                            account=ch,
                        )
                    if status != 429:
                        break
                    detail = raw.decode("utf-8", "replace")[:4000]
                    google_429 = parse_antigravity_429(detail)
                    delay = google_429.get("retry_after")
                    if (
                        not google_429.get("quota_exhausted")
                        and str(google_429.get("reason") or "").upper() == "RATE_LIMIT_EXCEEDED"
                        and isinstance(delay, (int, float))
                        and delay < 3
                        and short_retries < short_retry_limit
                    ):
                        short_retries += 1
                        await asyncio.sleep(float(delay))
                        continue
                    break

                if status == 200:
                    try:
                        payload, cache_items = _decode_with_cache_items(
                            json.loads(raw), model=model,
                            response_format=parsed.response_format,
                        )
                    except Exception as exc:
                        return await failed(
                            errors.json_error_openai(
                                502, errors.ErrTypeOpenAI.SERVER, str(exc),
                            ),
                            error_type="decode_error",
                            account=ch,
                        )
                    cache_cfg = _media_cache_settings()
                    cache_task = asyncio.create_task(asyncio.to_thread(
                        media_cache.cache_inline_base64,
                        cache_items,
                        cfg=cache_cfg,
                        provider="antigravity",
                        media_type="image",
                        action="generate",
                    ))
                    cache_result = await await_owned(cache_task)
                    generated_bytes = cache_result.total_bytes
                    if cache_result.status == "disabled":
                        limit = media_cache.file_limit(cache_cfg)
                        for item in cache_items:
                            try:
                                generated_bytes += len(media_cache.decode_base64(
                                    item["base64"], max_bytes=limit,
                                ))
                            except Exception:
                                pass
                    if cache_result.status == "failed":
                        print(
                            "[antigravity-images] cache failed "
                            f"request={request_id} type={cache_result.error_class or 'unknown'}"
                        )
                    cooldown.clear_on_success(key, model)
                    await _finish_log(
                        log_id,
                        status="success",
                        account_key=ch.account_key,
                        account_email=ch.email,
                        duration_ms=int((time.time() - started) * 1000),
                        image_count=len(payload["data"]),
                        requested_count=max(1, int(getattr(parsed, "requested_n", 1) or 1)),
                        usage=payload.get("usage"),
                        cached_media_count=len(cache_result.paths),
                        media_bytes=generated_bytes,
                        cache_paths=list(cache_result.paths),
                        cache_status=cache_result.status,
                        cache_error_class=cache_result.error_class,
                        http_status=200,
                    )
                    return JSONResponse(payload)
                detail = raw.decode("utf-8", "replace")[:4000]
                normalized_status = status if 400 <= status < 600 else 502
                last = errors.json_error_openai(
                    normalized_status,
                    errors.ErrTypeOpenAI.RATE_LIMIT if status == 429 else errors.ErrTypeOpenAI.SERVER,
                    detail,
                )
                last_account = ch
                if status == 429:
                    google_429 = google_429 or parse_antigravity_429(detail)
                    delay = google_429.get("retry_after")
                    if google_429.get("quota_exhausted"):
                        try:
                            manager.set_disabled_by_quota(ch.account_key, None)
                        except Exception as exc:
                            print(
                                "[antigravity-images] quota disable failed "
                                f"request={request_id} type={type(exc).__name__}"
                            )
                    elif isinstance(delay, (int, float)) and 3 <= delay < 300:
                        cooldown.record_error(
                            key, model, detail,
                            cooldown_until=int((time.time() + float(delay)) * 1000),
                        )
                    else:
                        cooldown.record_error(key, model, detail)
                    continue
                return await failed(last, error_type="upstream_error", account=ch)
            except Exception as exc:
                await _finish_log(
                    log_id,
                    status="failed",
                    account_key=ch.account_key,
                    account_email=ch.email,
                    duration_ms=int((time.time() - started) * 1000),
                    error_type=type(exc).__name__,
                    error_message="internal_error",
                    cache_status="disabled",
                )
                raise
            finally:
                concurrency.release(key)
        response = last or errors.json_error_openai(
            503, errors.ErrTypeOpenAI.SERVER,
            "all Antigravity image accounts are at capacity",
        )
        return await failed(
            response,
            error_type="upstream_error" if last else "at_capacity",
            account=last_account,
        )

    except asyncio.CancelledError:
        # Drain the original local side effects before the terminal update.
        # In particular, cancelling to_thread alone cannot stop a cache write.
        if log_task.done() and not log_task.cancelled():
            log_id = log_task.result()
        cache_fields: dict[str, Any] = {"cache_status": "disabled"}
        if cache_task is not None and cache_task.done() and not cache_task.cancelled():
            cached = cache_task.result()
            cache_fields = {
                "cache_status": cached.status,
                "cache_error_class": cached.error_class,
                "cache_paths": list(cached.paths),
                "cached_media_count": len(cached.paths),
                "media_bytes": cached.total_bytes,
            }
        await await_owned(_finish_log(
            log_id,
            status="cancelled",
            account_key=current_account.account_key if current_account else None,
            account_email=current_account.email if current_account else None,
            duration_ms=int((time.time() - started) * 1000),
            error_type="cancelled",
            error_message="image request cancelled",
            http_status=499,
            **cache_fields,
        ))
        raise
