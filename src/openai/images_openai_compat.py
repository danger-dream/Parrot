"""OpenAI Images API 兼容入口。

提供标准 OpenAI Images API：
  POST /v1/images/generations  {prompt, model?, n?, size?, response_format?, quality?, ...}
  POST /v1/images/edits        {prompt, model?, image[s]?, mask?, ...}

按 ``model`` 路由：配置的 Grok Imagine 图片模型透明转给 xAI OAuth，
其余模型继续复用 ``images_simple._execute_pipeline``（Codex Responses + image_generation tool）。

设计原则：
- 入参完整解析 OpenAI 标准字段（JSON + multipart）
- Grok Imagine 支持原生 ``n<=10``、URL/Base64 响应和最多 3 张编辑参考图
- GPT/Codex 管线的 ``n>1`` 显式降为 1，响应里带 ``parrot_warning``
- GPT/Codex 的 ``response_format=url`` 回填 data URL
- ``model``/``quality``/``background``/``output_format``/``moderation``/``style``/
  ``output_compression``/``partial_images``/``mask`` 透传到 image_generation tool
- ``generate`` 入口收到 ``mask`` 时记录 warning 日志（mask 仅 edit 有意义）
- 不实现流式：transit 角色，无渐进流式必要
- 全账号失败返回 429 时透传 ``Retry-After`` header
"""

from __future__ import annotations

import base64
import mimetypes
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from .. import apikey_limiter, auth, errors
from ..xai import imagine as xai_imagine
from .images_simple import (
    UpstreamImageError,
    _DEFAULTS,
    _execute_pipeline,
    _json_error,
    _normalize_image_input,
    settings,
)


_VALID_RESPONSE_FORMATS = ("b64_json", "url")
# 透传到 image_generation tool 的字符串字段（值非空才透传）。
_NATIVE_STR_FIELDS = (
    "quality", "background", "output_format",
    "moderation", "style", "input_fidelity",
)
# 透传到 image_generation tool 的整数字段。
_NATIVE_INT_FIELDS = ("output_compression", "partial_images")


# ── 内部解析结果 ───────────────────────────────────────────────────────────


@dataclass
class _ParsedRequest:
    prompt: str = ""
    model: str | None = None
    size: str | None = None
    response_format: str = "b64_json"
    response_format_explicit: bool = False
    requested_n: int = 1
    input_images: list[str] = field(default_factory=list)
    mask_url: str | None = None
    native_options: dict[str, Any] = field(default_factory=dict)
    xai_options: dict[str, Any] = field(default_factory=dict)


# ── 公共小工具 ─────────────────────────────────────────────────────────────


def _bad_request(msg: str, *, param: str | None = None) -> JSONResponse:
    return errors.json_error_openai(
        400, errors.ErrTypeOpenAI.INVALID_REQUEST, msg, param=param,
    )


def _coerce_int(value: Any, *, field_name: str, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        raise ValueError(f"invalid {field_name}: must be a number")
    if isinstance(value, int):
        n = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"invalid {field_name}: must be an integer")
        n = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"invalid {field_name}: empty")
        try:
            n = int(text)
        except ValueError as exc:
            raise ValueError(f"invalid {field_name}: {text!r}") from exc
    else:
        raise ValueError(f"invalid {field_name}: unsupported type")
    if minimum is not None and n < minimum:
        raise ValueError(f"invalid {field_name}: must be >= {minimum}")
    return n


def _coerce_bool(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True
        if text in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"invalid {field_name}: must be a boolean")


def _output_format_to_mime(fmt: str | None) -> str:
    f = (fmt or "png").strip().lower()
    if f in {"jpg", "jpeg"}:
        return "image/jpeg"
    if f == "webp":
        return "image/webp"
    if f == "gif":
        return "image/gif"
    return "image/png"


def _str_or_none(value: Any) -> str | None:
    """统一字段空值处理：仅当值为非空字符串时返回 strip 后的值，否则 None。"""
    if value is None:
        return None
    if not isinstance(value, str):
        return str(value).strip() or None
    text = value.strip()
    return text or None


# ── multipart 文件读取 ─────────────────────────────────────────────────────


async def _read_form_file(upload: Any, *, max_bytes: int, label: str) -> tuple[bytes, str]:
    if not hasattr(upload, "read"):
        raise ValueError(f"invalid {label}: not a file")
    raw = await upload.read()
    if len(raw) > max_bytes:
        raise ValueError(f"{label} is too large; max {max_bytes} bytes")
    filename = getattr(upload, "filename", "") or ""
    ctype = (
        getattr(upload, "content_type", None)
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    return raw, ctype


def _bytes_to_data_url(raw: bytes, content_type: str) -> str:
    return f"data:{content_type};base64," + base64.b64encode(raw).decode("ascii")


# ── 主解析 ─────────────────────────────────────────────────────────────────


async def _parse_body(request: Request, *, action: str, cfg: dict) -> _ParsedRequest:
    parsed = _ParsedRequest()
    max_image_bytes = int(cfg.get("maxInputImageBytes") or _DEFAULTS["maxInputImageBytes"])
    ctype = (request.headers.get("content-type") or "").lower()

    if ctype.startswith("multipart/form-data"):
        await _parse_multipart(request, parsed=parsed, action=action, max_image_bytes=max_image_bytes)
    else:
        await _parse_json(request, parsed=parsed, action=action, max_image_bytes=max_image_bytes)

    # generate 入口收到 mask 时给出 warning（mask 只对 edit 有意义）
    if action == "generate" and parsed.mask_url:
        print(
            "[images-openai-compat] WARNING: mask is only meaningful for /v1/images/edits; "
            "ignoring mask on generations endpoint"
        )
        parsed.mask_url = None

    return parsed


async def _parse_multipart(
    request: Request, *, parsed: _ParsedRequest, action: str, max_image_bytes: int,
) -> None:
    form = await request.form()

    prompt_val = form.get("prompt")
    if not isinstance(prompt_val, str):
        # multipart 也可能是 UploadFile；OpenAI 标准要求 string，拒掉。
        raise ValueError("invalid prompt: must be a string")
    parsed.prompt = prompt_val.strip()

    parsed.model = _str_or_none(form.get("model"))
    parsed.size = _str_or_none(form.get("size"))
    rf = _str_or_none(form.get("response_format"))
    if rf is not None:
        parsed.response_format = rf.lower()
        parsed.response_format_explicit = True
    if form.get("n") is not None and form.get("n") != "":
        parsed.requested_n = _coerce_int(form.get("n"), field_name="n", minimum=1)

    for fld in _NATIVE_STR_FIELDS:
        v = _str_or_none(form.get(fld))
        if v is not None:
            parsed.native_options[fld] = v
    for fld in _NATIVE_INT_FIELDS:
        v = form.get(fld)
        if v is not None and v != "":
            parsed.native_options[fld] = _coerce_int(v, field_name=fld, minimum=0)
    for fld in ("aspect_ratio", "resolution", "user"):
        v = _str_or_none(form.get(fld))
        if v is not None:
            parsed.xai_options[fld] = v

    if action == "edit":
        file_fields: list[Any] = []
        for key in ("image", "image[]", "images", "images[]"):
            # FormData 提供 getlist；其它 mapping 退化到单值 .get
            if hasattr(form, "getlist"):
                values: list[Any] = list(form.getlist(key))
            else:
                values = [form.get(key)]
            for v in values:
                if v is not None and v != "":
                    file_fields.append(v)
        for upload in file_fields:
            if hasattr(upload, "read"):
                raw, ctype_in = await _read_form_file(
                    upload, max_bytes=max_image_bytes, label="image",
                )
                parsed.input_images.append(_bytes_to_data_url(raw, ctype_in))
            else:
                parsed.input_images.append(
                    _normalize_image_input(str(upload), max_bytes=max_image_bytes)
                )

        mask = form.get("mask")
        if mask is not None and mask != "":
            if hasattr(mask, "read"):
                raw, ctype_in = await _read_form_file(
                    mask, max_bytes=max_image_bytes, label="mask",
                )
                parsed.mask_url = _bytes_to_data_url(raw, ctype_in)
            else:
                parsed.mask_url = _normalize_image_input(str(mask), max_bytes=max_image_bytes)


async def _parse_json(
    request: Request, *, parsed: _ParsedRequest, action: str, max_image_bytes: int,
) -> None:
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

    raw_prompt = body.get("prompt")
    if raw_prompt is not None and not isinstance(raw_prompt, str):
        raise ValueError("invalid prompt: must be a string")
    parsed.prompt = (raw_prompt or "").strip()

    parsed.model = _str_or_none(body.get("model"))
    parsed.size = _str_or_none(body.get("size"))
    rf = _str_or_none(body.get("response_format"))
    if rf is not None:
        parsed.response_format = rf.lower()
        parsed.response_format_explicit = True
    if body.get("n") is not None:
        parsed.requested_n = _coerce_int(body.get("n"), field_name="n", minimum=1)

    for fld in _NATIVE_STR_FIELDS:
        v = _str_or_none(body.get(fld))
        if v is not None:
            parsed.native_options[fld] = v
    for fld in _NATIVE_INT_FIELDS:
        if body.get(fld) is not None:
            parsed.native_options[fld] = _coerce_int(body.get(fld), field_name=fld, minimum=0)
    for fld in ("aspect_ratio", "resolution", "user"):
        v = _str_or_none(body.get(fld))
        if v is not None:
            parsed.xai_options[fld] = v
    if isinstance(body.get("storage_options"), dict):
        parsed.xai_options["storage_options"] = dict(body["storage_options"])

    if action == "edit":
        # 支持 image 单值 / image 数组 / images 数组
        raw_image = body.get("image")
        if isinstance(raw_image, list):
            for item in raw_image:
                if isinstance(item, str) and item.strip():
                    parsed.input_images.append(
                        _normalize_image_input(item, max_bytes=max_image_bytes)
                    )
        elif isinstance(raw_image, str) and raw_image.strip():
            parsed.input_images.append(
                _normalize_image_input(raw_image, max_bytes=max_image_bytes)
            )
        elif isinstance(raw_image, dict):
            iu = raw_image.get("image_url") or raw_image.get("url")
            if isinstance(iu, str) and iu.strip():
                parsed.input_images.append(
                    _normalize_image_input(iu, max_bytes=max_image_bytes)
                )
            elif raw_image.get("file_id"):
                raise ValueError(
                    "image.file_id is not supported (use image.url instead)"
                )

        raw_images = body.get("images")
        if isinstance(raw_images, list):
            for item in raw_images:
                if isinstance(item, str) and item.strip():
                    parsed.input_images.append(
                        _normalize_image_input(item, max_bytes=max_image_bytes)
                    )
                elif isinstance(item, dict):
                    iu = item.get("image_url") or item.get("url")
                    if isinstance(iu, str) and iu.strip():
                        parsed.input_images.append(
                            _normalize_image_input(iu, max_bytes=max_image_bytes)
                        )
                    elif item.get("file_id"):
                        raise ValueError(
                            "images[].file_id is not supported (use images[].image_url instead)"
                        )

    mask = body.get("mask")
    if isinstance(mask, str) and mask.strip():
        parsed.mask_url = _normalize_image_input(mask, max_bytes=max_image_bytes)
    elif isinstance(mask, dict):
        iu = mask.get("image_url") or mask.get("url")
        if isinstance(iu, str) and iu.strip():
            parsed.mask_url = _normalize_image_input(iu, max_bytes=max_image_bytes)
        elif mask.get("file_id"):
            raise ValueError("mask.file_id is not supported (use mask.image_url instead)")


# ── 响应构造 ───────────────────────────────────────────────────────────────


def _build_openai_response(
    *,
    result,
    parsed: _ParsedRequest,
    n_warning: bool,
) -> dict[str, Any]:
    data: list[dict[str, Any]] = []
    for img in result.images:
        b64 = img.get("b64_json") or ""
        item: dict[str, Any] = {}
        if parsed.response_format == "url":
            mime = _output_format_to_mime(img.get("output_format"))
            item["url"] = f"data:{mime};base64,{b64}"
        else:
            item["b64_json"] = b64
        if img.get("revised_prompt"):
            item["revised_prompt"] = img["revised_prompt"]
        data.append(item)

    first = result.images[0] if result.images else {}
    payload: dict[str, Any] = {
        "created": int(time.time()),
        "data": data,
    }
    if first.get("output_format"):
        payload["output_format"] = first["output_format"]
    if first.get("size"):
        payload["size"] = first["size"]
    if parsed.native_options.get("quality"):
        payload["quality"] = parsed.native_options["quality"]
    if parsed.native_options.get("background"):
        payload["background"] = parsed.native_options["background"]

    # 响应 model = 实际使用的 tool_model；客户端请求的 model 单独放在 parrot_requested_model 里
    # 避免误导客户端以为跑的是它请求的 dall-e-3/gpt-image-1 之类。
    payload["model"] = result.tool_model
    if parsed.model and parsed.model != result.tool_model:
        payload["parrot_requested_model"] = parsed.model

    if result.usage:
        payload["usage"] = result.usage
    if n_warning:
        payload["parrot_warning"] = (
            f"requested n={parsed.requested_n} but upstream Codex image_generation "
            "tool produces 1 image per call; effective n=1"
        )
    return payload


# ── 主入口 ─────────────────────────────────────────────────────────────────


async def _run_handler(request: Request, *, action: str) -> JSONResponse:
    cfg = settings()
    if not cfg.get("enabled", True):
        return _json_error(403, errors.ErrTypeOpenAI.PERMISSION, "image generation is disabled")

    key_name, allowed_models, err = auth.validate(request.headers)
    if err:
        return _json_error(401, errors.ErrTypeOpenAI.AUTH, err)
    if not auth.images_allowed(key_name):
        return _json_error(
            403, errors.ErrTypeOpenAI.PERMISSION,
            "this API key is not allowed to use image endpoints",
        )

    try:
        parsed = await _parse_body(request, action=action, cfg=cfg)
    except ValueError as exc:
        return _bad_request(str(exc))

    if not parsed.prompt:
        return _bad_request("prompt is required", param="prompt")

    max_prompt = int(cfg.get("maxPromptChars") or _DEFAULTS["maxPromptChars"])
    if len(parsed.prompt) > max_prompt:
        return _bad_request(f"prompt is too long; max {max_prompt} chars", param="prompt")

    if parsed.response_format not in _VALID_RESPONSE_FORMATS:
        return _bad_request(
            f"invalid response_format {parsed.response_format!r}; must be one of {_VALID_RESPONSE_FORMATS}",
            param="response_format",
        )

    if action == "edit" and not parsed.input_images:
        return _bad_request(
            "image is required for edits (use 'image' or 'images[]')",
            param="image",
        )

    # The standard Images routes are shared.  Only explicitly configured Grok
    # Imagine models select xAI; every other model keeps the existing GPT/Codex
    # image pipeline unchanged.
    if xai_imagine.is_xai_image_model(parsed.model):
        assert key_name is not None
        return await xai_imagine.handle_image(
            parsed,
            action=action,
            key_name=key_name,
            allowed_models=allowed_models,
        )
    if xai_imagine.looks_like_xai_image_model(parsed.model):
        return _bad_request(
            f"unsupported xAI image model {parsed.model!r}",
            param="model",
        )

    n_warning = False
    if parsed.requested_n > 1:
        n_warning = True
        print(
            f"[images-openai-compat] requested n={parsed.requested_n} downgraded to 1 "
            "(Codex image_generation tool returns 1 image per call)"
        )

    try:
        result = await _execute_pipeline(
            action=action,
            key_name=key_name,
            prompt=parsed.prompt,
            size=parsed.size,
            input_image_urls=parsed.input_images or None,
            mask_url=parsed.mask_url,
            native_options=parsed.native_options or None,
            cfg=cfg,
        )
    except UpstreamImageError as exc:
        headers: dict[str, str] = {}
        # 全账号失败后回传 429 时透传 Retry-After，方便客户端做指数退避。
        if exc.status_code == 429 and exc.retry_after is not None:
            headers["Retry-After"] = str(exc.retry_after)
        return errors.json_error_openai(
            exc.status_code, exc.err_type, exc.message,
        ) if not headers else JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": exc.message, "type": exc.err_type, "code": None, "param": None}},
            headers=headers,
        )

    return JSONResponse(
        _build_openai_response(result=result, parsed=parsed, n_warning=n_warning)
    )


async def handle_generations(request: Request) -> JSONResponse:
    """POST /v1/images/generations - OpenAI-compatible image generation."""
    return await _run_handler(request, action="generate")


async def handle_edits(request: Request) -> JSONResponse:
    """POST /v1/images/edits - OpenAI-compatible image edit."""
    return await _run_handler(request, action="edit")
