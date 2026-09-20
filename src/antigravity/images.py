"""Antigravity adapter for Parrot's unified OpenAI Images runtime.

This module owns only provider-specific request/response translation. Candidate
selection, permissions, caching, delivery URLs, logs and ambiguous-outcome policy
remain in :mod:`src.openai.images_runtime`.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import time
from typing import Any

import httpx

from .. import image_artifacts, image_catalog, media_cache, media_config, network
from ..async_owned import await_owned
from ..oauth import antigravity as ag_provider
from ..providers.antigravity_codec import unwrap_cloud_code, wrap_cloud_code

_MAX_RESPONSE_BYTES = 110 * 1024 * 1024
_SUPPORTED_ASPECTS = {
    "1:1": 1.0,
    "2:3": 2 / 3,
    "3:2": 3 / 2,
    "3:4": 3 / 4,
    "4:3": 4 / 3,
    "4:5": 4 / 5,
    "5:4": 5 / 4,
    "9:16": 9 / 16,
    "16:9": 16 / 9,
    "21:9": 21 / 9,
}


def is_antigravity_image_model(model: str | None) -> bool:
    name = str(model or "").strip()
    return bool(name) and name in media_config.model_map("image").get("antigravity", [])


def looks_like_antigravity_image_model(model: str | None) -> bool:
    name = str(model or "").strip().lower()
    return bool(name) and "gemini" in name and "image" in name


def is_source(source: Any) -> bool:
    # Provider labels are also used by generic API channels. Only the canonical
    # OAuth account key identifies the Cloud Code transport owned here.
    return str(getattr(source, "key", "")).startswith("oauth:antigravity:")


def _aspect_ratio(parsed: Any) -> str | None:
    explicit = str((getattr(parsed, "xai_options", {}) or {}).get("aspect_ratio") or "").strip()
    if explicit:
        if explicit not in _SUPPORTED_ASPECTS:
            raise ValueError(
                "Antigravity aspect_ratio must be one of "
                + ", ".join(_SUPPORTED_ASPECTS)
            )
        return explicit
    size = str(getattr(parsed, "size", None) or "").strip().lower()
    if not size or size == "auto":
        return None
    try:
        width_text, height_text = size.split("x", 1)
        ratio = int(width_text) / int(height_text)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError("size must be auto or WIDTHxHEIGHT") from exc
    # The common runtime performs the exact final contain/padding operation. Ask
    # Gemini for the nearest native canvas so composition changes stay minimal.
    return min(_SUPPORTED_ASPECTS, key=lambda key: abs(math.log(ratio / _SUPPORTED_ASPECTS[key])))


def validate_request(parsed: Any, *, action: str) -> None:
    if action not in {"generate", "edit"}:
        raise ValueError("Antigravity image action must be generate or edit")
    if action == "edit" and not getattr(parsed, "input_images", None):
        raise ValueError("Antigravity image editing requires at least one reference image")
    options = getattr(parsed, "native_options", {}) or {}
    if options.get("background") == "transparent":
        raise ValueError(
            "Antigravity transparent background is not supported by this adapter "
            "(verified opaque JPEG output without alpha); use background=auto/opaque "
            "or choose a compatible image source"
        )
    if options.get("moderation") not in (None, "auto"):
        raise ValueError(
            "Antigravity does not expose moderation controls; use moderation=auto "
            "or choose a compatible image source"
        )
    quality = str(options.get("quality") or "auto").strip().lower()
    if quality not in {"auto", "low", "medium", "standard", "hd", "high"}:
        raise ValueError(
            "Antigravity quality must be auto, low, medium, standard, hd or high"
        )
    _aspect_ratio(parsed)


async def prepare_edit_inputs(parsed: Any) -> Any:
    """Resolve and validate references before any paid AG generation attempt."""
    import copy

    prepared = copy.copy(parsed)
    references: list[str] = []
    for index, reference in enumerate(getattr(parsed, "input_images", None) or []):
        raw = await image_artifacts.reference_bytes(reference)
        _name, raw, mime = image_artifacts.multipart_image(raw, f"image-{index}")
        references.append(f"data:{mime};base64," + base64.b64encode(raw).decode("ascii"))
    prepared.input_images = references
    if getattr(parsed, "mask_url", None):
        raw = await image_artifacts.reference_bytes(parsed.mask_url)
        _name, raw, mime = image_artifacts.multipart_image(raw, "mask", force_png=True)
        prepared.mask_url = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
    return prepared


def _reference_part(reference: str) -> dict[str, Any]:
    raw, mime = media_cache.decode_data_url(
        reference,
        max_bytes=media_cache.HARD_FILE_LIMIT,
    )
    _name, raw, mime = image_artifacts.multipart_image(raw, "reference")
    return {
        "inlineData": {
            "mimeType": mime,
            "data": base64.b64encode(raw).decode("ascii"),
        }
    }


def build_request(parsed: Any, *, prompt: str, n: int, action: str) -> dict[str, Any]:
    validate_request(parsed, action=action)
    if n != 1:
        raise ValueError("Antigravity adapter performs one requested generation per upstream call")
    options = getattr(parsed, "native_options", {}) or {}
    generation: dict[str, Any] = {"responseModalities": ["IMAGE"]}
    image_config: dict[str, Any] = {}
    aspect = _aspect_ratio(parsed)
    if aspect:
        image_config["aspectRatio"] = aspect
    quality = str(options.get("quality") or "auto").strip().lower()
    if quality in {"low", "medium", "standard"}:
        image_config["imageSize"] = "1K"
    elif quality in {"hd", "high"}:
        image_config["imageSize"] = "2K"
    if image_config:
        generation["imageConfig"] = image_config
    parts: list[dict[str, Any]] = [{"text": prompt}]
    if action == "edit":
        for index, reference in enumerate(parsed.input_images, start=1):
            parts.append({"text": f"Reference image {index}:"})
            parts.append(_reference_part(reference))
        if getattr(parsed, "mask_url", None):
            parts.append({"text": (
                "Edit mask for reference image 1: transparent pixels are the editable area; "
                "opaque pixels must remain unchanged."
            )})
            parts.append(_reference_part(parsed.mask_url))
    return {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": generation,
    }


def _usage(meta: Any) -> dict[str, Any] | None:
    if not isinstance(meta, dict):
        return None

    def count(name: str, default: int = 0) -> int:
        try:
            return max(0, int(meta.get(name) if meta.get(name) is not None else default))
        except (TypeError, ValueError):
            return default

    prompt = count("promptTokenCount")
    candidates = count("candidatesTokenCount")
    total = count("totalTokenCount", prompt + candidates)
    usage: dict[str, Any] = {
        "input_tokens": prompt,
        "output_tokens": candidates,
        "total_tokens": total,
    }
    cached = count("cachedContentTokenCount")
    thoughts = count("thoughtsTokenCount")
    if cached:
        usage["input_tokens_details"] = {"cached_tokens": cached}
    if thoughts:
        usage["output_tokens_details"] = {"reasoning_tokens": thoughts}
    return usage


def normalize_response(payload: Any, *, model: str) -> dict[str, Any]:
    data = unwrap_cloud_code(payload)
    candidates = data.get("candidates") if isinstance(data, dict) else None
    images: list[dict[str, str]] = []
    for candidate in candidates or []:
        parts = (
            ((candidate.get("content") or {}).get("parts") or [])
            if isinstance(candidate, dict) else []
        )
        for part in parts:
            inline = (
                part.get("inlineData") or part.get("inline_data")
                if isinstance(part, dict) else None
            )
            if not isinstance(inline, dict):
                continue
            encoded = str(inline.get("data") or "")
            if not encoded:
                continue
            # Validate the alphabet now; actual image decoding and size bounds are
            # enforced by the common artifact normalizer.
            try:
                base64.b64decode(encoded, validate=True)
            except Exception as exc:
                raise ValueError("Antigravity returned invalid base64 image data") from exc
            images.append({"b64_json": encoded})
    if not images:
        raise ValueError("Antigravity completed without a decodable image")
    result: dict[str, Any] = {
        "created": int(time.time()),
        "model": model,
        "data": images,
    }
    usage = _usage(
        data.get("usageMetadata") or data.get("usage_metadata")
        if isinstance(data, dict) else None
    )
    if usage:
        result["usage"] = usage
    return result


async def _read_bounded(
    response: httpx.Response,
    *,
    timing: Any = None,
    round_timeouts: Any = None,
) -> bytes:
    """Collect decoded response bytes under both size and route-round limits."""
    declared = response.headers.get("content-length")
    if declared:
        try:
            if int(declared) > _MAX_RESPONSE_BYTES:
                raise ValueError("Antigravity image response is too large")
        except ValueError as exc:
            raise ValueError("invalid or oversized Antigravity Content-Length") from exc
    chunks: list[bytes] = []
    total = 0
    if timing is None:
        iterator = response.aiter_bytes()
        async for chunk in iterator:
            total += len(chunk)
            if total > _MAX_RESPONSE_BYTES:
                raise ValueError("Antigravity image response is too large")
            chunks.append(chunk)
        return b"".join(chunks)

    from ..transports import next_nonempty_http_chunk

    timing.start_response_body_wait()
    iterator = response.aiter_bytes()
    while True:
        try:
            chunk = await next_nonempty_http_chunk(iterator, timing, round_timeouts)
        except StopAsyncIteration:
            timing.mark_io_complete()
            return b"".join(chunks)
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            raise ValueError("Antigravity image response is too large")
        chunks.append(chunk)


def _decoded_body_headers(headers: Any) -> dict[str, str]:
    """Remove representation/framing headers after ``aiter_bytes`` decoded it."""
    result = dict(headers or {})
    for key in list(result):
        if str(key).lower() in {"content-encoding", "content-length", "transfer-encoding"}:
            result.pop(key, None)
    return result


async def _fingerprinted_post(
    channel: Any,
    *,
    model: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout_seconds: float,
) -> tuple[int, dict[str, str], bytes, httpx.Request]:
    """Use the shared route runtime so images get the same curl/SS lifecycle."""
    from ..channel.base import UpstreamRequest
    from ..transports import (
        BUSINESS_TIMEOUT_OUTCOMES,
        TRANSPORT_TIMEOUT_OUTCOMES,
        BusinessTimeoutError,
        close_proxy_client,
        close_response_context,
        finalize_opened_http_response,
        open_response_with_proxy_chain,
    )

    upstream_request = UpstreamRequest(url=url, headers=headers, body=body)
    opened = await open_response_with_proxy_chain(
        channel=channel,
        resolved_model=model,
        upstream_req=upstream_request,
        connect_timeout=min(15.0, timeout_seconds),
        first_byte_timeout=timeout_seconds,
        idle_timeout=timeout_seconds,
        total_timeout=timeout_seconds,
        response_mode="non_stream",
        # Images have their own media log. Avoid creating an orphan request-log row.
        request_id=None,
        proxy_purpose="oauth_antigravity",
    )
    if not opened.ok:
        error = opened.error
        detail = str(getattr(error, "error_detail", None) or "Antigravity image transport failed")
        outcome = str(getattr(error, "outcome", "transport_error") or "transport_error")
        if outcome in BUSINESS_TIMEOUT_OUTCOMES or outcome in TRANSPORT_TIMEOUT_OUTCOMES:
            raise httpx.TimeoutException(detail)
        raise httpx.TransportError(detail)

    response = opened.response
    assert response is not None

    async def finish_and_close(outcome: str, detail: str | None = None) -> None:
        try:
            await finalize_opened_http_response(opened, outcome, detail)
        finally:
            try:
                await close_response_context(opened.ctx)
            finally:
                await close_proxy_client(opened.proxy_client)

    try:
        raw = await _read_bounded(
            response,
            timing=opened.timing,
            round_timeouts=opened.round_timeouts,
        )
        status = response.status_code
        response_headers = dict(response.headers)
        request_obj = response.request
    except asyncio.CancelledError:
        await await_owned(finish_and_close("cancelled", "Antigravity image request cancelled"))
        raise
    except BusinessTimeoutError as exc:
        await await_owned(finish_and_close(exc.outcome, str(exc)))
        raise httpx.TimeoutException(str(exc), request=response.request) from exc
    except httpx.TimeoutException as exc:
        await await_owned(finish_and_close("transport_timeout", str(exc)))
        raise
    except Exception as exc:
        await await_owned(finish_and_close("transport_error", str(exc)))
        raise
    else:
        outcome = "success" if 200 <= status < 300 else "http_error"
        await await_owned(finish_and_close(outcome))
        return status, response_headers, raw, request_obj


async def request(
    source: Any, parsed: Any, *, prompt: str, n: int, action: str, cfg: dict,
) -> httpx.Response:
    """Perform one provider POST and return an OpenAI-shaped HTTP response."""
    validate_request(parsed, action=action)
    account_id, _account = image_catalog.current_oauth_account(source)
    from ..channel import registry
    channel = registry.get_channel(source.key)
    if channel is None or getattr(channel, "state_key", None) != source.state_key:
        raise ValueError("selected Antigravity image account generation was deleted")
    if not channel.supports_media_model("image", source.upstream):
        raise ValueError("selected Antigravity image source is no longer available")
    headers = await channel.build_media_headers()
    # A refresh may replace the persisted account generation. Revalidate before I/O.
    current_id, _account = image_catalog.current_oauth_account(source)
    if current_id != account_id:
        raise ValueError("selected Antigravity image account changed during token refresh")
    gemini = build_request(parsed, prompt=prompt, n=n, action=action)
    envelope = wrap_cloud_code(
        gemini,
        model=source.upstream,
        project_id=channel.project_id,
        stream=False,
        session_id="",
    )
    url = f"{channel.base_url}/{ag_provider.API_VERSION}:generateContent"
    timeout_seconds = float(cfg.get("requestTimeoutSeconds") or 180)
    body = json.dumps(
        envelope, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    if str(getattr(channel, "tls_fingerprint", None) or "").strip():
        status, response_headers, raw, request_obj = await _fingerprinted_post(
            channel,
            model=source.upstream,
            url=url,
            headers=headers,
            body=body,
            timeout_seconds=timeout_seconds,
        )
    else:
        timeout = httpx.Timeout(timeout_seconds, connect=15)
        async with network.async_client(
            timeout=timeout,
            proxy_purpose="oauth_antigravity",
            proxy_channel=source.key,
            proxy_model=source.upstream,
            follow_redirects=False,
        ) as client:
            async with client.stream(
                "POST", url, headers=headers, content=body,
            ) as upstream:
                raw = await _read_bounded(upstream)
                status = upstream.status_code
                response_headers = dict(upstream.headers)
                request_obj = upstream.request

    # Both httpx and curl return decoded bytes from ``aiter_bytes``. Reusing the
    # upstream representation header would make the in-memory response decode a
    # second time. Framing headers are stale for the normalized body as well.
    response_headers = _decoded_body_headers(response_headers)
    if status < 200 or status >= 300:
        return httpx.Response(
            status,
            headers=response_headers,
            content=raw,
            request=request_obj,
        )
    try:
        payload = json.loads(raw)
        normalized = normalize_response(payload, model=source.upstream)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(str(exc)) from exc
    response_headers.pop("content-length", None)
    response_headers["content-type"] = "application/json"
    return httpx.Response(
        200,
        headers=response_headers,
        json=normalized,
        request=request_obj,
    )
