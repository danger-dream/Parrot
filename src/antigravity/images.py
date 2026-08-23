"""Antigravity implementation for Parrot's shared OpenAI Images routes."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
from fastapi.responses import JSONResponse, Response

from .. import channel_state, concurrency, config, cooldown, errors, load_balancing, network, scorer
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


def _decode(obj: dict[str, Any], *, model: str, response_format: str) -> dict[str, Any]:
    data = unwrap_cloud_code(obj)
    candidates = data.get("candidates") if isinstance(data, dict) else None
    out: list[dict[str, Any]] = []
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
    if not out: raise ValueError("Antigravity returned no decodable image")
    payload: dict[str, Any] = {"created": int(time.time()), "data": out, "model": model}
    usage = data.get("usageMetadata") or data.get("usage_metadata") if isinstance(data, dict) else None
    if usage: payload["usage"] = usage
    return payload


async def handle_image(parsed: Any, *, action: str, key_name: str, allowed_models: list[str]) -> Response:
    model = str(parsed.model or "").strip()
    if action != "generate": return _bad("Antigravity image models do not support /images/edits", "model")
    if allowed_models and model not in allowed_models: return errors.json_error_openai(403, errors.ErrTypeOpenAI.PERMISSION, "model is not allowed for this API key")
    try: gemini = _build_request(parsed)
    except ValueError as exc: return _bad(str(exc))
    channels = _eligible(model)
    if not channels: return errors.json_error_openai(503, errors.ErrTypeOpenAI.SERVER, f"no available Antigravity OAuth account for image model {model}")
    last: Response | None = None
    retry_cfg = config.get()
    short_retry_limit = (
        transient_retry_limit(retry_cfg)
        if transient_retry_allowed("antigravityRateLimit", retry_cfg)
        else 0
    )
    for ch in channels:
        key = channel_state.effect_key(ch)
        if not await concurrency.try_acquire(key): continue
        try:
            manager = __import__("src.oauth_manager", fromlist=["ensure_valid_token"])
            token = await manager.ensure_valid_token(ch.account_key)
            envelope = wrap_cloud_code(gemini, model=model, project_id=ch.project_id, stream=False, session_id="")
            headers = ch._build_headers(token, stream=False)
            url = f"{ch.base_url}/{ag_provider.API_VERSION}:generateContent"
            short_retries = 0
            google_429: dict[str, Any] = {}
            while True:
                try:
                    async with network.async_client(timeout=httpx.Timeout(180.0, connect=15.0), proxy_purpose="oauth_antigravity", proxy_channel=ch.key, proxy_model=model, follow_redirects=False) as client:
                        async with client.stream("POST", url, headers=headers, content=json.dumps(envelope, separators=(",", ":")).encode()) as response:
                            raw = await _read_bounded(response)
                            status = response.status_code
                except httpx.TimeoutException:
                    return errors.json_error_openai(504, errors.ErrTypeOpenAI.TIMEOUT, "Antigravity image generation timed out; it was not retried")
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
                try: payload = _decode(json.loads(raw), model=model, response_format=parsed.response_format)
                except Exception as exc: return errors.json_error_openai(502, errors.ErrTypeOpenAI.SERVER, str(exc))
                cooldown.clear(key, model)
                return JSONResponse(payload)
            detail = raw.decode("utf-8", "replace")[:4000]
            last = errors.json_error_openai(status if 400 <= status < 600 else 502, errors.ErrTypeOpenAI.RATE_LIMIT if status == 429 else errors.ErrTypeOpenAI.SERVER, detail)
            if status == 429:
                google_429 = google_429 or parse_antigravity_429(detail)
                delay = google_429.get("retry_after")
                if google_429.get("quota_exhausted"):
                    try:
                        manager.set_disabled_by_quota(ch.account_key, None)
                    except Exception as exc:
                        print(f"[antigravity-images] quota disable failed for {ch.account_key}: {exc}")
                elif isinstance(delay, (int, float)) and 3 <= delay < 300:
                    cooldown.record_error(
                        key, model, detail,
                        cooldown_until=int((time.time() + float(delay)) * 1000),
                    )
                else:
                    cooldown.record_error(key, model, detail)
                continue
            return last
        finally:
            concurrency.release(key)
    return last or errors.json_error_openai(503, errors.ErrTypeOpenAI.SERVER, "all Antigravity image accounts are at capacity")
