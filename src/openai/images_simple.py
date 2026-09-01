"""Parrot 封装版图片生成/编辑接口。

外部不兼容 OpenAI 标准 Images API，仅提供：
  POST /v1/images/generate  {prompt, size?}
  POST /v1/images/edit      {prompt, image, size?}

内部通过 Codex Responses 的 image_generation tool 调用 ChatGPT。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

from .. import (
    apikey_limiter,
    auth,
    config,
    errors,
    image_db,
    network,
    oauth_manager,
    state_db,
)
from ..oauth import normalize_provider
from ..oauth import openai as openai_provider
from ..oauth_ids import account_key as make_account_key
from .codex_constants import (
    CODEX_ORIGINATOR,
    CODEX_ROUTING_HINT_HEADER,
    build_codex_routing_hint,
    codex_cli_user_agent,
    codex_cli_version,
)

CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"

_DEFAULTS = {
    "enabled": True,
    "mainModel": "gpt-5.4-mini",
    "toolModel": "gpt-image-2",
    "disabledAccounts": [],
    "cacheEnabled": False,
    "cachePath": "images",
    "cacheRetentionDays": 0,
    "cacheMaxBytes": 1073741824,
    "accountCooldownSeconds": 300,
    "requestTimeoutSeconds": 180,
    "maxPromptChars": 4000,
    "maxInputImageBytes": 20 * 1024 * 1024,
}

# 图片模块独立临时冷却：只影响图片生成，不影响普通 OpenAI/Codex API。
_IMAGE_COOLDOWNS: dict[str, float] = {}


@dataclass
class UpstreamImageError(Exception):
    message: str
    status_code: int = 502
    err_type: str = errors.ErrTypeOpenAI.SERVER
    retryable: bool = False
    cooldown: bool = False
    user_visible: bool = False
    force_refresh: bool = False
    # 上游 429/5xx 返回的 retry-after 秒数；只在所有账号都失败时才回传给客户端。
    retry_after: int | None = None

    def __str__(self) -> str:
        return self.message


def settings() -> dict:
    raw = (config.get().get("images") or {})
    out = dict(_DEFAULTS)
    for k, v in raw.items():
        out[k] = v
    return out


def _json_error(status: int, err_type: str, msg: str) -> JSONResponse:
    return errors.json_error_openai(status, err_type, msg)


def _prompt_preview(prompt: str) -> str:
    p = " ".join(str(prompt or "").split())
    return p[:200]


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()[:16]


def _disabled_accounts_set(cfg: dict) -> set[str]:
    vals = cfg.get("disabledAccounts") or []
    out: set[str] = set()
    for value in vals:
        raw = str(value).strip()
        if not raw:
            continue
        out.add(raw.lower())
        if raw.startswith("oauth:"):
            out.add(raw[len("oauth:"):].lower())
    return out


def _cooldown_active(account_key: str) -> bool:
    until = _IMAGE_COOLDOWNS.get(account_key)
    if not until:
        return False
    if until <= time.time():
        _IMAGE_COOLDOWNS.pop(account_key, None)
        return False
    return True


def _set_cooldown(account_key: str, seconds: int) -> None:
    if seconds > 0:
        _IMAGE_COOLDOWNS[account_key] = time.time() + seconds


def list_image_accounts(include_disabled: bool = False) -> list[dict]:
    cfg = settings()
    disabled = _disabled_accounts_set(cfg)
    out: list[dict] = []
    for acc in oauth_manager.list_accounts():
        if normalize_provider(acc.get("provider")) != "openai":
            continue
        ak = make_account_key(acc)
        email = str(acc.get("email") or "")
        image_disabled = (
            ak.lower() in disabled
            or f"oauth:{ak}".lower() in disabled
            or email.lower() in disabled
            or f"openai:{email}".lower() in disabled
        )
        row = {
            "account": acc,
            "account_key": ak,
            "email": email,
            "enabled": bool(acc.get("enabled", True)) and not bool(acc.get("disabled_reason")),
            "image_disabled": image_disabled,
            "image_cooldown_until": _IMAGE_COOLDOWNS.get(ak, 0),
            "missing_account_id": not bool(acc.get("workspace_id") or acc.get("chatgpt_account_id") or acc.get("account_id")),
        }
        if include_disabled or (row["enabled"] and not image_disabled and not row["missing_account_id"] and not _cooldown_active(ak)):
            out.append(row)
    return out


def _candidate_accounts() -> list[dict]:
    return [x for x in list_image_accounts(include_disabled=False)]


# OpenAI Images API 兼容入口允许透传到 image_generation tool 的字段集合。
_NATIVE_TOOL_STR_FIELDS = (
    "size", "quality", "background", "output_format",
    "moderation", "style",
)
_NATIVE_TOOL_INT_FIELDS = ("output_compression", "partial_images")


def _build_payload(*, action: str, prompt: str, main_model: str, tool_model: str,
                   size: str | None = None, images: list[str] | None = None,
                   native_options: dict[str, Any] | None = None,
                   mask_url: str | None = None) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "type": "image_generation",
        "action": action,
        "model": tool_model,
    }
    if size is not None and str(size).strip():
        # 只有用户显式提供 size 时才发送，避免改变上游默认值。
        tool["size"] = str(size).strip()

    if native_options:
        for field in _NATIVE_TOOL_STR_FIELDS:
            value = native_options.get(field)
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            tool[field] = text
        for field in _NATIVE_TOOL_INT_FIELDS:
            value = native_options.get(field)
            if value is None:
                continue
            try:
                tool[field] = int(value)
            except (TypeError, ValueError):
                continue

    if mask_url and str(mask_url).strip():
        tool["input_image_mask"] = {"image_url": str(mask_url).strip()}

    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    for img in images or []:
        if str(img).strip():
            content.append({"type": "input_image", "image_url": str(img).strip()})

    return {
        "instructions": "",
        "stream": True,
        "reasoning": {"effort": "medium", "summary": "auto"},
        "parallel_tool_calls": True,
        "include": ["reasoning.encrypted_content"],
        "model": main_model,
        "store": False,
        "tool_choice": {"type": "image_generation"},
        "input": [{"type": "message", "role": "user", "content": content}],
        "tools": [tool],
    }


def _build_headers(
    access_token: str,
    account_id: str,
    model: str | None = None,
) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "User-Agent": codex_cli_user_agent(),
        "Version": codex_cli_version(),
        "Session_id": str(uuid.uuid4()),
        "Accept": "text/event-stream",
        "Connection": "Keep-Alive",
        "Originator": CODEX_ORIGINATOR,
        "Chatgpt-Account-Id": account_id,
    }
    routing_hint = build_codex_routing_hint(model)
    if routing_hint:
        headers[CODEX_ROUTING_HINT_HEADER] = routing_hint
    return headers


def _classify_error(status: int, body: str, *, retry_after: int | None = None) -> UpstreamImageError:
    msg = body.strip() or f"upstream HTTP {status}"
    lower = msg.lower()

    policy_markers = (
        "policy", "moderation", "safety", "unsafe", "disallowed", "sexual",
        "explicit", "violence", "content_filter", "content policy",
        "could not be generated",
    )
    quota_markers = (
        "quota", "rate limit", "rate_limit", "usage limit", "too many requests",
        "capacity", "temporarily unavailable", "try again later",
    )
    permission_markers = (
        "not entitled", "not supported", "does not have access", "permission",
        "image generation", "unsupported", "not available",
    )

    if any(x in lower for x in policy_markers):
        return UpstreamImageError(
            msg[:1000], status_code=400, err_type=errors.ErrTypeOpenAI.INVALID_REQUEST,
            retryable=False, user_visible=True,
        )
    if status == 401:
        return UpstreamImageError(
            msg[:1000], status_code=401, err_type=errors.ErrTypeOpenAI.AUTH,
            retryable=True, cooldown=True, force_refresh=True,
        )
    if status in (403, 404) or any(x in lower for x in permission_markers):
        return UpstreamImageError(
            msg[:1000], status_code=status or 403, err_type=errors.ErrTypeOpenAI.PERMISSION,
            retryable=True, cooldown=True,
        )
    if status == 429 or any(x in lower for x in quota_markers):
        return UpstreamImageError(
            msg[:1000], status_code=429, err_type=errors.ErrTypeOpenAI.RATE_LIMIT,
            retryable=True, cooldown=True, retry_after=retry_after,
        )
    if status >= 500 or status in (408, 504):
        return UpstreamImageError(
            msg[:1000], status_code=status or 502,
            err_type=errors.classify_http_status_openai(status or 502),
            retryable=True, cooldown=False, retry_after=retry_after,
        )
    return UpstreamImageError(
        msg[:1000], status_code=status or 400,
        err_type=errors.classify_http_status_openai(status or 400),
        retryable=False,
    )


def _update_codex_quota(account_key: str, email: str, headers: httpx.Headers | dict) -> None:
    try:
        snap = openai_provider.parse_rate_limit_headers(dict(headers))
        if snap:
            normalized = openai_provider.normalize_codex_snapshot(snap)
            state_db.quota_save_openai_snapshot(account_key, snap, normalized, email=email)
    except Exception as exc:
        print(f"[images] quota snapshot update failed for {account_key}: {exc}")


async def _iter_sse_events(resp: httpx.Response):
    buf = ""
    async for chunk in resp.aiter_text():
        if not chunk:
            continue
        buf += chunk
        while "\n\n" in buf:
            frame, buf = buf.split("\n\n", 1)
            data_lines = []
            for line in frame.splitlines():
                line = line.rstrip("\r")
                if line.startswith("data:"):
                    data_lines.append(line[5:].strip())
            if not data_lines:
                continue
            data = "\n".join(data_lines).strip()
            if not data or data == "[DONE]":
                continue
            try:
                yield json.loads(data)
            except json.JSONDecodeError:
                continue


def _patch_completed(event: dict[str, Any], by_index: dict[int, dict], fallback: list[dict]) -> dict[str, Any]:
    if event.get("type") != "response.completed":
        return event
    response = event.get("response")
    if not isinstance(response, dict):
        return event
    output = response.get("output")
    if isinstance(output, list) and output:
        return event
    patched = [by_index[i] for i in sorted(by_index)] + list(fallback)
    if patched:
        response["output"] = patched
    return event


def _extract_images(event: dict[str, Any]) -> tuple[list[dict], dict | None, int]:
    response = event.get("response") or {}
    results: list[dict] = []
    total_bytes = 0
    for item in response.get("output") or []:
        if item.get("type") != "image_generation_call":
            continue
        b64 = str(item.get("result") or "").strip()
        if not b64:
            continue
        fmt = str(item.get("output_format") or "png").strip().lower() or "png"
        try:
            size_bytes = len(base64.b64decode(b64, validate=False))
        except Exception:
            size_bytes = 0
        total_bytes += size_bytes
        results.append({
            "b64_json": b64,
            "revised_prompt": item.get("revised_prompt") or "",
            "output_format": fmt,
            "size": item.get("size") or "",
            "bytes": size_bytes,
        })
    usage = response.get("tool_usage", {}).get("image_gen") or response.get("usage")
    return results, usage if isinstance(usage, dict) else None, total_bytes


async def _call_upstream_once(account_row: dict, payload: dict, *, timeout_s: int,
                              refresh_first: bool = False) -> tuple[list[dict], dict | None, int]:
    acc = account_row["account"]
    ak = account_row["account_key"]
    email = account_row.get("email") or ""
    account_id = str(acc.get("workspace_id") or acc.get("chatgpt_account_id") or acc.get("account_id") or "").strip()
    if not account_id:
        raise UpstreamImageError("OpenAI OAuth account missing chatgpt_account_id/workspace_id", 403, errors.ErrTypeOpenAI.PERMISSION, retryable=True, cooldown=True)

    access_token = await (oauth_manager.force_refresh(ak) if refresh_first else oauth_manager.ensure_valid_token(ak))
    headers = _build_headers(access_token, account_id, str(payload.get("model") or ""))
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    by_index: dict[int, dict] = {}
    fallback: list[dict] = []

    try:
        timeout = httpx.Timeout(connect=15.0, read=float(timeout_s), write=30.0, pool=15.0)
        async with network.async_client(
            timeout=timeout,
            http2=False,
            proxy_purpose="oauth_openai",
            proxy_channel=f"oauth:{ak}",
            proxy_model=str(payload.get("model") or ""),
        ) as client:
            async with client.stream("POST", CODEX_RESPONSES_URL, headers=headers, content=body) as resp:
                _update_codex_quota(ak, email, resp.headers)
                if resp.status_code >= 400:
                    text = await resp.aread()
                    ra_raw = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
                    ra_int: int | None = None
                    if ra_raw:
                        try:
                            ra_int = max(0, int(float(str(ra_raw).strip())))
                        except (ValueError, TypeError):
                            ra_int = None
                    raise _classify_error(
                        resp.status_code,
                        text.decode("utf-8", "replace"),
                        retry_after=ra_int,
                    )
                async for event in _iter_sse_events(resp):
                    typ = str(event.get("type") or "")
                    if typ == "response.output_item.done":
                        item = event.get("item")
                        if isinstance(item, dict):
                            idx = event.get("output_index")
                            if isinstance(idx, int):
                                by_index[idx] = item
                            else:
                                fallback.append(item)
                    elif typ == "response.completed":
                        event = _patch_completed(event, by_index, fallback)
                        images, usage, total_bytes = _extract_images(event)
                        if not images:
                            raise UpstreamImageError(
                                "upstream completed without image output", 502,
                                errors.ErrTypeOpenAI.SERVER, retryable=True,
                            )
                        return images, usage, total_bytes
                    elif typ in {"response.failed", "response.incomplete"}:
                        raise _classify_error(400, json.dumps(event, ensure_ascii=False))
    except UpstreamImageError:
        raise
    except httpx.TimeoutException as exc:
        raise UpstreamImageError(f"upstream timeout: {exc}", 504, errors.ErrTypeOpenAI.TIMEOUT, retryable=True) from exc
    except Exception as exc:
        raise UpstreamImageError(str(exc)[:1000], 502, errors.ErrTypeOpenAI.SERVER, retryable=True) from exc

    raise UpstreamImageError("stream ended before response.completed", 502, errors.ErrTypeOpenAI.SERVER, retryable=True)


def _cache_root(cfg: dict) -> Path:
    raw = str(cfg.get("cachePath") or "images").strip() or "images"
    if os.path.isabs(raw):
        root = Path(raw).resolve()
    else:
        root = (Path(config.DATA_DIR) / raw).resolve()
        data_root = Path(config.DATA_DIR).resolve()
        try:
            root.relative_to(data_root)
        except ValueError as exc:
            raise ValueError("cachePath escapes data directory") from exc
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ext_for(fmt: str) -> str:
    f = (fmt or "png").lower().strip()
    if f in {"jpeg", "jpg"}:
        return "jpg"
    if f == "webp":
        return "webp"
    return "png"


def _save_cached_images(images: list[dict], *, action: str, cfg: dict) -> tuple[list[str], int, int]:
    if not cfg.get("cacheEnabled"):
        return [], 0, 0
    root = _cache_root(cfg)
    day = time.strftime("%Y%m%d", time.localtime())
    out_dir = root / day
    out_dir.mkdir(parents=True, exist_ok=True)

    paths: list[str] = []
    total = 0
    for idx, img in enumerate(images):
        b64 = img.get("b64_json") or ""
        raw = base64.b64decode(b64, validate=False)
        total += len(raw)
        ext = _ext_for(img.get("output_format") or "png")
        path = out_dir / f"{action}-{int(time.time())}-{uuid.uuid4().hex[:10]}-{idx}.{ext}"
        path.write_bytes(raw)
        paths.append(str(path))
    _cleanup_cache(root, cfg)
    return paths, len(paths), total


def _cleanup_cache(root: Path, cfg: dict) -> None:
    try:
        retention_days = int(cfg.get("cacheRetentionDays") or 0)
        max_bytes = int(cfg.get("cacheMaxBytes") or 0)
    except Exception:
        retention_days, max_bytes = 0, 0
    # The same cache is shared by GPT images and xAI Imagine images/videos.
    suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm", ".mov", ".m4v"}
    files: list[Path] = []
    for p in root.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink() and p.suffix.lower() in suffixes:
                files.append(p)
        except OSError:
            continue
    now = time.time()
    if retention_days > 0:
        cutoff = now - retention_days * 86400
        kept = []
        for p in files:
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
                else:
                    kept.append(p)
            except OSError:
                pass
        files = kept
    if max_bytes <= 0:
        return
    stats = []
    total = 0
    for p in files:
        try:
            st = p.stat()
            total += st.st_size
            stats.append((st.st_mtime, st.st_size, p))
        except OSError:
            pass
    if total <= max_bytes:
        return
    stats.sort(key=lambda x: x[0])
    # 每轮删除最老 20%，若仍超限继续删除。
    while total > max_bytes and stats:
        n = max(1, (len(stats) + 4) // 5)
        batch, stats = stats[:n], stats[n:]
        for _, sz, p in batch:
            try:
                p.unlink(missing_ok=True)
                total -= sz
            except OSError:
                pass


def _normalize_image_input(value: str, *, max_bytes: int) -> str:
    s = str(value or "").strip()
    if not s:
        raise ValueError("image is required")
    if s.startswith("data:"):
        b64 = s.split(",", 1)[1] if "," in s else ""
        approx = len(b64) * 3 // 4
        if approx > max_bytes:
            raise ValueError(f"image is too large; max {max_bytes} bytes")
        return s
    # 兼容直接传裸 base64，默认按 png 包装。
    if re.fullmatch(r"[A-Za-z0-9+/=\s]+", s) and len(s) > 100:
        compact = "".join(s.split())
        approx = len(compact) * 3 // 4
        if approx > max_bytes:
            raise ValueError(f"image is too large; max {max_bytes} bytes")
        return "data:image/png;base64," + compact
    # 允许直接透传 http(s) 图片 URL。
    if s.startswith("http://") or s.startswith("https://"):
        return s
    raise ValueError("image must be data URL, raw base64, or http(s) URL")


async def _read_body(request: Request, *, action: str, cfg: dict) -> tuple[str, str | None, str | None]:
    ctype = (request.headers.get("content-type") or "").lower()
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        prompt = str(form.get("prompt") or "").strip()
        size = str(form.get("size") or "").strip() or None
        image_url = None
        if action == "edit":
            upload = form.get("image")
            if upload is not None and hasattr(upload, "read"):
                raw = await upload.read()
                if len(raw) > int(cfg.get("maxInputImageBytes") or _DEFAULTS["maxInputImageBytes"]):
                    raise ValueError("image is too large")
                mt = getattr(upload, "content_type", None) or mimetypes.guess_type(getattr(upload, "filename", ""))[0] or "application/octet-stream"
                image_url = f"data:{mt};base64," + base64.b64encode(raw).decode("ascii")
            else:
                image_url = _normalize_image_input(str(form.get("image") or form.get("image_url") or ""), max_bytes=int(cfg.get("maxInputImageBytes") or _DEFAULTS["maxInputImageBytes"]))
        return prompt, size, image_url

    try:
        body = await request.json()
    except (
        apikey_limiter.RequestBodyTooLarge,
        apikey_limiter.QueuedBodySpoolError,
    ):
        raise
    except Exception as exc:
        raise ValueError(f"invalid json: {exc}") from exc
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    prompt = str(body.get("prompt") or "").strip()
    size_raw = body.get("size")
    size = str(size_raw).strip() if size_raw is not None and str(size_raw).strip() else None
    image_url = None
    if action == "edit":
        image_url = _normalize_image_input(
            str(body.get("image") or body.get("image_url") or ""),
            max_bytes=int(cfg.get("maxInputImageBytes") or _DEFAULTS["maxInputImageBytes"]),
        )
    return prompt, size, image_url


# === pipeline+handlers below ===

# === 公共管线 + 多入口包装 ===


@dataclass
class _PipelineResult:
    images: list[dict]
    usage: dict | None
    request_id: str
    main_model: str
    tool_model: str
    account_email: str
    duration_ms: int
    cached: bool


async def _execute_pipeline(
    *,
    action: str,
    key_name: str | None,
    prompt: str,
    size: str | None,
    input_image_urls: list[str] | None = None,
    mask_url: str | None = None,
    native_options: dict[str, Any] | None = None,
    cfg: dict | None = None,
) -> _PipelineResult:
    """图片生成公共管线：调上游 + failover + cooldown + 日志落库。

    成功返回 _PipelineResult；失败抛 UpstreamImageError，外层入口负责包装。
    """
    cfg = cfg if cfg is not None else settings()
    main_model = str(cfg.get("mainModel") or _DEFAULTS["mainModel"]).strip()
    tool_model = str(cfg.get("toolModel") or _DEFAULTS["toolModel"]).strip()
    request_id = str(uuid.uuid4())
    log_id = await asyncio.to_thread(
        image_db.start_call,
        request_id=request_id,
        api_key_name=key_name,
        action=action,
        main_model=main_model,
        tool_model=tool_model,
        size=size,
        prompt_preview=_prompt_preview(prompt),
        prompt_hash=_prompt_hash(prompt),
    )

    payload = _build_payload(
        action=action, prompt=prompt, main_model=main_model, tool_model=tool_model,
        size=size, images=input_image_urls or None,
        native_options=native_options, mask_url=mask_url,
    )

    started = time.time()
    last_err: UpstreamImageError | None = None
    accounts = _candidate_accounts()
    if not accounts:
        await asyncio.to_thread(
            image_db.finish_call, log_id,
            status="failed", duration_ms=int((time.time() - started) * 1000),
            error_type="no_account", error_message="no available OpenAI OAuth account for images",
        )
        raise UpstreamImageError(
            "no available OpenAI OAuth account for images", 503,
            errors.ErrTypeOpenAI.SERVER, retryable=False, user_visible=True,
        )

    for row in accounts:
        ak = row["account_key"]
        email = row.get("email") or ""
        await asyncio.to_thread(image_db.mark_attempt, log_id, account_key=ak, account_email=email)
        try:
            try_refresh = False
            for sub_try in range(2):
                attempt_started = time.time()
                attempt_id = await asyncio.to_thread(
                    image_db.start_attempt,
                    log_id,
                    request_id=request_id,
                    account_key=ak,
                    account_email=email,
                )
                try:
                    images, usage, total_bytes = await _call_upstream_once(
                        row, payload,
                        timeout_s=int(cfg.get("requestTimeoutSeconds") or _DEFAULTS["requestTimeoutSeconds"]),
                        refresh_first=try_refresh,
                    )
                    try:
                        cache_paths, cached_count, cached_bytes = await asyncio.to_thread(
                            _save_cached_images, images, action=action, cfg=cfg,
                        )
                    except Exception as cache_exc:
                        print(f"[images] cache save failed for request {request_id}: {cache_exc}")
                        cache_paths, cached_count, cached_bytes = [], 0, 0
                    duration_ms = int((time.time() - started) * 1000)
                    attempt_duration_ms = int((time.time() - attempt_started) * 1000)
                    image_bytes = cached_bytes or total_bytes
                    await asyncio.to_thread(
                        image_db.finish_attempt,
                        attempt_id,
                        status="success",
                        duration_ms=attempt_duration_ms,
                        image_count=len(images),
                        image_bytes=image_bytes,
                    )
                    await asyncio.to_thread(
                        image_db.finish_call, log_id,
                        status="success", account_key=ak, account_email=email,
                        duration_ms=duration_ms, image_count=len(images),
                        cached_images=cached_count, image_bytes=image_bytes,
                        cache_paths=cache_paths, usage=usage,
                    )
                    return _PipelineResult(
                        images=images,
                        usage=usage,
                        request_id=request_id,
                        main_model=main_model,
                        tool_model=tool_model,
                        account_email=email,
                        duration_ms=duration_ms,
                        cached=bool(cache_paths),
                    )
                except UpstreamImageError as exc:
                    last_err = exc
                    await asyncio.to_thread(
                        image_db.finish_attempt,
                        attempt_id,
                        status="failed",
                        duration_ms=int((time.time() - attempt_started) * 1000),
                        error_type=exc.err_type,
                        error_message=exc.message,
                    )
                    if exc.force_refresh and not try_refresh and sub_try == 0:
                        try_refresh = True
                        continue
                    raise
        except UpstreamImageError as exc:
            last_err = exc
            if exc.cooldown:
                _set_cooldown(ak, int(cfg.get("accountCooldownSeconds") or _DEFAULTS["accountCooldownSeconds"]))
            if exc.user_visible or not exc.retryable:
                break
            continue

    duration_ms = int((time.time() - started) * 1000)
    err_obj = last_err or UpstreamImageError("image generation failed")
    await asyncio.to_thread(
        image_db.finish_call, log_id,
        status="failed", duration_ms=duration_ms,
        error_type=err_obj.err_type, error_message=err_obj.message,
    )
    raise err_obj


async def _handle(request: Request, *, action: str) -> JSONResponse:
    """Parrot 私有 schema 入口：/v1/images/generate 与 /v1/images/edit。"""
    cfg = settings()
    if not cfg.get("enabled", True):
        return _json_error(403, errors.ErrTypeOpenAI.PERMISSION, "image generation is disabled")

    key_name, _, err = auth.validate(request.headers)
    if err:
        return _json_error(401, errors.ErrTypeOpenAI.AUTH, err)
    if not auth.images_allowed(key_name):
        return _json_error(403, errors.ErrTypeOpenAI.PERMISSION, "this API key is not allowed to use image endpoints")

    try:
        prompt, size, image_url = await _read_body(request, action=action, cfg=cfg)
    except ValueError as exc:
        return _json_error(400, errors.ErrTypeOpenAI.INVALID_REQUEST, str(exc))

    if not prompt:
        return _json_error(400, errors.ErrTypeOpenAI.INVALID_REQUEST, "prompt is required")
    max_prompt = int(cfg.get("maxPromptChars") or _DEFAULTS["maxPromptChars"])
    if len(prompt) > max_prompt:
        return _json_error(400, errors.ErrTypeOpenAI.INVALID_REQUEST, f"prompt is too long; max {max_prompt} chars")

    try:
        result = await _execute_pipeline(
            action=action,
            key_name=key_name,
            prompt=prompt,
            size=size,
            input_image_urls=[image_url] if image_url else None,
            cfg=cfg,
        )
    except UpstreamImageError as exc:
        return _json_error(exc.status_code, exc.err_type, exc.message)

    return JSONResponse({
        "id": result.request_id,
        "object": f"parrot.image.{action}",
        "created": int(time.time()),
        "action": action,
        "model": result.main_model,
        "image_model": result.tool_model,
        "account": result.account_email,
        "data": result.images,
        "usage": result.usage,
        "cached": result.cached,
        "duration_ms": result.duration_ms,
    })


async def handle_generate(request: Request) -> JSONResponse:
    return await _handle(request, action="generate")


async def handle_edit(request: Request) -> JSONResponse:
    return await _handle(request, action="edit")
