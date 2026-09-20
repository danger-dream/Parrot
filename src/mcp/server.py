"""MCP 服务端装配与工具处理器。

设计要点：

- **复用而非复制**：搜索调 ``search_service``，图片调 ``images_openapi_compat``，
  视频调 ``xai.imagine`` —— 与 HTTP 入口共用同一条实现链路，因此权限、渠道选择、
  日志、缓存和媒体 URL 生成天然一致。
- **按 Key 实时生成工具表**：``tools/list`` 每次按当前配置与该 Key 的授权计算，
  工具说明里的可选值随之更新（引擎/模型增删后无需重启）。
- **失败必须可被模型理解**：所有可预期的拒绝用 ``ToolError`` 抛出（SDK 会把消息
  带给模型）；普通异常会被 SDK 吞成通用错误，模型看不到原因。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Optional

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.server import ServerRequestContext
from mcp.server.mcpserver.exceptions import ToolError
from starlette.responses import Response

from .. import apikey_limiter, auth, config, image_artifacts, log_db, search_service
from ..async_owned import await_owned
from . import catalog, policy, request_adapter

SERVER_NAME = "Parrot"
SERVER_INSTRUCTIONS = (
    "Parrot 提供的搜索与媒体工具。网络搜索、图片生成/编辑、视频生成均由 Parrot "
    "统一代理；返回值中的 URL 可直接访问。"
)


# ── 请求上下文提取 ──────────────────────────────────────────────────────────


def _key_name(ctx: ServerRequestContext) -> Optional[str]:
    request = ctx.request
    if request is None:
        return None
    state = request.scope.get("state") or {}
    return state.get("parrot_key_name")


def _base_url(ctx: ServerRequestContext) -> str:
    request = ctx.request
    if request is None:
        return ""
    try:
        return str(request.base_url)
    except Exception:
        return ""


def _client_info(ctx: ServerRequestContext) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(client name, client version, protocol version)。"""
    name = version = None
    meta = ctx.meta if isinstance(ctx.meta, dict) else None
    if meta:
        info = meta.get("io.modelcontextprotocol/clientInfo")
        if isinstance(info, dict):
            name = str(info.get("name") or "") or None
            version = str(info.get("version") or "") or None
    return name, version, getattr(ctx, "protocol_version", None)


# ── 工具 schema ─────────────────────────────────────────────────────────────

_SEARCH_RESULT_ITEM = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "url": {"type": "string"},
        "snippet": {"type": "string"},
        "published_at": {"type": "string"},
    },
}


def _search_schema() -> dict:
    x_source = catalog.x_search_source_id()
    return {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "网页或 X（Twitter）搜索关键词。"},
            "max_results": {
                "type": "integer", "minimum": 1, "maximum": 20,
                "description": (
                    f"最终返回结果条数上限；省略则用服务端默认值。source={x_source} 时，"
                    "该值不限制 xAI 内部获取的帖子数量或相应计费。"
                ),
            },
            "freshness": {
                "type": "string", "enum": ["day", "week", "month", "year"],
                "description": (
                    f"只返回该时间范围内发布的内容；source={x_source} 时转换为原生 from_date。"
                    "不能与 from_date/to_date 同时使用。"
                ),
            },
            "allowed_domains": {
                "type": "array", "items": {"type": "string"},
                "description": f"只在这些域名内搜索；source={x_source} 不支持，传入会报错。",
            },
            "blocked_domains": {
                "type": "array", "items": {"type": "string"},
                "description": f"排除这些域名；source={x_source} 不支持，传入会报错。",
            },
            "allowed_x_handles": {
                "type": "array", "items": {"type": "string"}, "maxItems": 20,
                "description": (
                    f"仅 source={x_source}：只搜索这些 X 用户发布的内容，最多20个；用户名可带或不带 @。"
                    "不能与 excluded_x_handles 同时使用。"
                ),
            },
            "excluded_x_handles": {
                "type": "array", "items": {"type": "string"}, "maxItems": 20,
                "description": (
                    f"仅 source={x_source}：排除这些 X 用户发布的内容，最多20个；用户名可带或不带 @。"
                    "不能与 allowed_x_handles 同时使用。"
                ),
            },
            "from_date": {
                "type": "string", "format": "date",
                "description": f"仅 source={x_source}：搜索起始日期，格式 YYYY-MM-DD；不能与 freshness 同时使用。",
            },
            "to_date": {
                "type": "string", "format": "date",
                "description": f"仅 source={x_source}：搜索结束日期，格式 YYYY-MM-DD；不能与 freshness 同时使用。",
            },
            "enable_image_understanding": {
                "type": "boolean",
                "description": f"仅 source={x_source}：允许 Grok 理解 X 帖子中的图片，可能增加图像 Token 费用。",
            },
            "enable_video_understanding": {
                "type": "boolean",
                "description": f"仅 source={x_source}：允许 Grok 理解 X 帖子中的视频，可能增加媒体 Token 费用。",
            },
            **catalog.common_properties("web_search"),
        },
        "required": ["query"],
    }


def _fetch_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要抓取的公开 http(s) 地址。"},
            "max_chars": {
                "type": "integer", "minimum": 1000,
                "description": "返回正文的字符上限；省略则用服务端默认值。",
            },
            **catalog.common_properties("web_fetch"),
        },
        "required": ["url"],
    }


def _image_schema(*, edit: bool) -> dict:
    properties: dict[str, Any] = {
        "prompt": {"type": "string", "description": "图片描述或修改要求。"},
        "size": {"type": "string", "description": "如 1024x1024；省略或 auto 表示由上游决定。"},
        "output_format": {"type": "string", "enum": ["png", "jpeg", "webp"]},
        "background": {"type": "string", "enum": ["auto", "opaque", "transparent"]},
        "quality": {"type": "string", "enum": ["low", "medium", "high", "auto"]},
        **catalog.common_properties("image_edit" if edit else "image_generate"),
    }
    if edit:
        properties["images"] = {
            "type": "array", "items": {"type": "string"},
            "description": "输入图片，可为 data URL、裸 base64 或 http(s) 地址。",
        }
        properties["mask"] = {
            "type": "string", "description": "可选遮罩；透明区域会被重新生成。",
        }
        required = ["prompt", "images"]
    else:
        properties["n"] = {
            "type": "integer", "minimum": 1, "maximum": 10,
            "description": "生成张数，默认 1。",
        }
        required = ["prompt"]
    return {"type": "object", "properties": properties, "required": required}


def _video_generate_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "视频内容描述。"},
            "image": {"type": "string", "description": "可选的起始图（data URL 或 http(s) 地址）。"},
            "aspect_ratio": {"type": "string", "description": "如 16:9、9:16。"},
            "resolution": {"type": "string", "description": "如 720p、1080p。"},
            "size": {"type": "string", "description": "兼容写法；等价于 aspect_ratio + resolution。"},
            "duration": {"type": "number", "description": "视频时长（秒）。"},
            **catalog.common_properties("video_generate"),
        },
        "required": ["prompt"],
    }


def _video_status_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "request_id": {"type": "string", "description": "video_generate 返回的任务 ID。"},
            **catalog.common_properties("video_status"),
        },
        "required": ["request_id"],
    }


_SCHEMA_BUILDERS: dict[str, Any] = {
    "web_search": _search_schema,
    "web_fetch": _fetch_schema,
    "image_generate": lambda: _image_schema(edit=False),
    "image_edit": lambda: _image_schema(edit=True),
    "video_generate": _video_generate_schema,
    "video_status": _video_status_schema,
}


def build_tool(tool_name: str, key_name: Optional[str] = None) -> types.Tool:
    """按当前配置与 Key 授权构造工具定义（说明与候选值采用同一口径）。"""
    schema = _SCHEMA_BUILDERS[tool_name]()
    if catalog.accepts_source(tool_name):
        schema["properties"]["source"] = catalog.source_property(tool_name, key_name)
    return types.Tool(
        name=tool_name,
        description=catalog.render_description(tool_name, key_name),
        input_schema=schema,
    )


# ── 工具实现 ────────────────────────────────────────────────────────────────


def _pick_source(arguments: dict) -> Optional[str]:
    """从统一参数里取出 source；auto/空 表示不指定。"""
    value = str(arguments.get("source") or "").strip()
    if not value or value.lower() == catalog.AUTO:
        return None
    return value


def _apply_source(arguments: dict, *, kind: str, tool_name: str = "web_search",
                  key_name: Optional[str] = None) -> Optional[str]:
    """校验并消费统一 source 参数，返回指定的来源 id。

    不可用时抛 ToolError 并带上当前可用列表，让模型能自行纠正（而不是逐个试）。
    """
    source = _pick_source(arguments)
    if source is None:
        return None
    if kind == "search":
        available = catalog.available_engines(tool_name)
        if source not in available:
            if catalog.is_x_search_source_id(source):
                raise ToolError(
                    "X（Twitter）搜索当前不可用：需要至少一个已接入、启用且状态可用的 xAI OAuth 搜索账户。"
                )
            raise ToolError(
                f"搜索引擎 {source!r} 当前不可用。当前可用：{', '.join(available) or '无'}。"
            )
        return source
    available = catalog.media_sources(kind, key_name)
    if source not in available:
        raise ToolError(
            f"模型 {source!r} 当前不可用。当前可用：{', '.join(available) or '无'}。"
        )
    return source


def _auto_model(kind: str, key_name: Optional[str] = None) -> Optional[str]:
    """未指定来源时，按当前配置挑一个可用模型。

    ``source`` 的契约是"省略或 auto = 按当前配置自动选择"，但下游
    （images_runtime / imagine）只接受**具体的模型名**，没有 auto 这个概念：
    传 auto 会被 images_openai_compat 判为 unknown image model。
    因此这里替下游把 auto 解析成一个真实模型。

    优先用模型中心配置的默认模型；没配或它当前不可用时，回落到可用列表首项
    （availability 由 image_catalog/video_models 判定，因此默认模型失效不会
    把调用卡死，而是按可用列表继续）。

    返回 None 表示当前没有任何可用模型；调用方据此给出可读的错误，而不是把
    auto 透下去换回一句"unknown image model"。
    """
    if kind in ("image", "video"):
        options = catalog.media_sources(kind, key_name)
        configured = _configured_default_model(kind)
    else:
        options = catalog.available_engines()
        configured = ""
    if configured and configured in options:
        return configured
    return options[0] if options else None


def _configured_default_model(kind: str) -> str:
    """读取模型中心里该媒体类型的默认模型；出错时按未配置处理。

    直接读配置（``images.defaultModel`` / ``videos.defaultModel``），不构造管理
    上下文——这里只是执行期的值读取，没有管理动作。默认模型只是一个偏好：
    配置不可读不应该让工具本身失败。
    """
    try:
        from .. import media_config

        return str(media_config.settings(kind).get("defaultModel") or "")
    except Exception:
        return ""


def _requested_model(arguments: dict, *, kind: str, key_name: Optional[str] = None) -> str:
    """媒体工具实际使用的模型：统一由 source 指定。

    历史上图片/视频工具另有一个 model 参数，与 source 语义重复且 source 只校验
    不生效。现在只保留 source（schema 里也只暴露它），此处兼容读取旧的 model，
    让升级期间已发出的调用不会突然失效。

    未指定时解析成当前可用的具体模型——下游不认识 auto。
    """
    source = _apply_source(arguments, kind=kind, key_name=key_name)
    if source:
        return source
    legacy = str(arguments.get("model") or "").strip()
    if legacy and legacy.lower() != catalog.AUTO:
        return legacy
    resolved = _auto_model(kind, key_name)
    if resolved:
        return resolved
    raise ToolError(
        "当前没有可用且获准的模型；请检查媒体来源与当前 API Key 的模型授权。"
    )


def _timeout_override(arguments: dict) -> Optional[float]:
    value = arguments.get("timeout_seconds")
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            raise TypeError("boolean is not a timeout")
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ToolError("timeout_seconds 必须是数字。") from exc
    if not 0.1 <= seconds <= 600:
        raise ToolError("timeout_seconds 须在 0.1 到 600 秒之间。")
    return seconds


def _search_arguments(arguments: dict) -> dict:
    """把 MCP 参数翻译成 search_service 的统一参数。"""
    payload: dict[str, Any] = {"query": arguments.get("query")}
    for key in ("max_results", "freshness", "allowed_domains", "blocked_domains",
                "allowed_x_handles", "excluded_x_handles", "from_date", "to_date",
                "enable_image_understanding", "enable_video_understanding", "language", "country"):
        if arguments.get(key) is not None:
            payload[key] = arguments[key]
    return {key: value for key, value in payload.items() if value is not None}


def _extract_arguments(arguments: dict) -> dict:
    return {"url": arguments.get("url")}


def _fetch_max_chars(arguments: dict) -> Optional[int]:
    value = arguments.get("max_chars")
    if value is not None and (type(value) is not int or value < 1000):
        raise ToolError("max_chars 必须是至少 1000 的整数。")
    return value


async def _run_search(tool_name: str, arguments: dict, *, request_id: str) -> tuple[dict, dict]:
    """执行搜索/抓取，返回 (给模型看的结果, 服务端保留的遥测字段)。

    来源标识与每次尝试遥测**不交给模型**（``_model_visible_search_result`` 会剥掉），
    但日志需要它们，所以在剥除前取出来单独返回。
    """
    source = _apply_source(arguments, kind="search", tool_name=tool_name)
    max_chars = _fetch_max_chars(arguments) if tool_name == "web_fetch" else None
    x_source = catalog.x_search_source_id() if tool_name == "web_search" else None
    if tool_name == "web_search" and source == x_source:
        result = await search_service.x_search(
            _search_arguments(arguments), request_id=request_id, origin="mcp",
        )
    elif tool_name == "web_search":
        result = await search_service.search(
            _search_arguments(arguments), request_id=request_id,
            backend_id=source, origin="mcp",
        )
    else:
        result = await search_service.extract(
            _extract_arguments(arguments), request_id=request_id,
            backend_id=source, origin="mcp",
        )
    attempts = result.get("attempts") if isinstance(result.get("attempts"), list) else []
    # 优先取最终成功那一次尝试的来源：搜索可以合法地跨来源重试，最后一次才是实际出结果的。
    final = next((a for a in reversed(attempts) if isinstance(a, dict)), {})
    telemetry = {
        # MCP telemetry records the source the caller selected. The lower-level
        # search-call log still records the concrete xAI backend/account attempt.
        "source_id": (x_source if source == x_source else
                      result.get("backend_id") or final.get("backend_id")),
        "source_type": result.get("provider") or final.get("provider"),
        "result_count": len(result.get("results") or []) if isinstance(result.get("results"), list) else 0,
    }
    if result.get("model"):
        telemetry["model"] = result["model"]

    # 只把模型有权看到的字段交出去：来源标识、每次尝试遥测与计费事实留在服务端。
    from ..local_web_tools import _model_visible_search_result

    visible = _model_visible_search_result(result, "search" if tool_name == "web_search" else "extract")
    content = visible.get("content")
    if max_chars is not None and isinstance(content, str) and len(content) > max_chars:
        visible["content"] = content[:max_chars]
        visible["truncated"] = True
        visible["warnings"] = list(dict.fromkeys([
            *(visible.get("warnings") or []), "content_truncated_to_mcp_max_chars",
        ]))
    return visible, telemetry


def _key_secret(key_name: Optional[str]) -> str:
    """取出该 Key 的密钥串，用于驱动既有 HTTP 处理器自身的鉴权。"""
    entry = auth.api_key_entry(key_name)
    if not entry:
        raise ToolError("当前 API Key 已失效，请重新获取。")
    secret = str(entry.get("key") or "")
    if not secret:
        raise ToolError("当前 API Key 已失效，请重新获取。")
    return secret


def _synthesized_request(payload: dict, *, key_name: Optional[str], base_url: str, path: str):
    secret = _key_secret(key_name)
    return request_adapter.synthesize(
        payload, path=path, base_url=base_url,
        headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json"},
    )


def _decode_json_response(response: Response) -> dict:
    try:
        return json.loads(bytes(response.body or b"{}"))
    except Exception:
        return {}


def _response_error_message(body: dict) -> str:
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("type") or "上游调用失败")
    return "上游调用失败"


class _PartialToolResult(ToolError):
    """A failed batch that still owns generated, deliverable media."""

    def __init__(self, message: str, result: dict, extra: dict):
        super().__init__(message)
        self.result = result
        self.extra = extra


async def _run_image(tool_name: str, arguments: dict, *, key_name: Optional[str],
                     base_url: str) -> tuple[dict, dict[str, Any]]:
    """调用既有图片处理器；返回 (模型可见结果, 日志附加字段)。"""
    from ..openai import images_openai_compat

    payload: dict[str, Any] = {
        "model": _requested_model(arguments, kind="image", key_name=key_name),
        "prompt": arguments.get("prompt"),
        # 统一走 URL 交付：既有处理器会把图片发布为 Parrot 资源 URL。
        "response_format": "url",
    }
    for key in ("size", "output_format", "background", "quality", "n"):
        if arguments.get(key) is not None:
            payload[key] = arguments[key]
    if tool_name == "image_edit":
        images = arguments.get("images")
        if not isinstance(images, list) or not images:
            raise ToolError("image_edit 需要 images 参数（至少一张图片）。")
        payload["image"] = images
        if arguments.get("mask"):
            payload["mask"] = arguments["mask"]

    path = "/v1/images/generations" if tool_name == "image_generate" else "/v1/images/edits"
    request = _synthesized_request(payload, key_name=key_name, base_url=base_url, path=path)
    action = "generate" if tool_name == "image_generate" else "edit"
    response = await images_openai_compat._run_handler(request, action=action)
    body = _decode_json_response(response)
    data = body.get("data") if isinstance(body.get("data"), list) else []
    urls = [str(item.get("url")) for item in data if isinstance(item, dict) and item.get("url")]
    if not urls:
        if response.status_code >= 400:
            raise ToolError(_response_error_message(body))
        raise ToolError("上游没有返回可用的图片。")
    result = {
        "model": body.get("model"),
        "images": urls,
        "count": len(urls),
        "size": body.get("size"),
        "expires_in_hint": "生成的图片 URL 有时效，请尽快保存或转存。",
    }
    extra = {
        "model": body.get("model"),
        "result_count": len(urls),
        "media_tokens": [url.rsplit("/", 1)[-1] for url in urls],
    }
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    extra["input_tokens"] = int(usage.get("input_tokens") or 0)
    extra["output_tokens"] = int(usage.get("output_tokens") or 0)
    if response.status_code >= 400:
        message = _response_error_message(body)
        result.update(complete=False, error={"message": message})
        raise _PartialToolResult(message, result, extra)
    return result, extra


async def _run_video_create(arguments: dict, *, key_name: Optional[str],
                            base_url: str) -> tuple[dict, dict[str, Any]]:
    from ..xai import imagine

    payload: dict[str, Any] = {
        "model": _requested_model(arguments, kind="video", key_name=key_name),
        "prompt": arguments.get("prompt"),
    }
    for key in ("image", "aspect_ratio", "resolution", "size", "duration"):
        if arguments.get(key) is not None:
            payload[key] = arguments[key]

    request = _synthesized_request(payload, key_name=key_name, base_url=base_url,
                                   path="/v1/videos/generations")
    response = await imagine.handle_video_create(request, action="generate")
    body = _decode_json_response(response)
    if response.status_code >= 400:
        raise ToolError(_response_error_message(body))

    request_id = str(body.get("request_id") or "").strip()
    if not request_id:
        raise ToolError("上游未返回视频任务 ID，请稍后重试。")
    result = {
        "request_id": request_id,
        "status": body.get("status"),
        "model": body.get("model"),
        "hint": "用 video_status 传入该 request_id 查询进度；完成后会返回视频 URL。",
    }
    return result, {"video_request_id": request_id, "model": body.get("model"), "result_count": 1}


async def _publish_video(request_id: str, upstream_url: str, *, key_name: Optional[str],
                         base_url: str) -> Optional[str]:
    """把已完成的视频转存为 Parrot 资源 URL；失败时返回 None 由调用方回退。"""
    if not upstream_url or not base_url:
        return None
    from .. import media_config, state_db
    from ..channel import registry
    from ..channel.xai_oauth_channel import XAIOAuthChannel
    from ..xai import imagine

    binding = state_db.xai_video_job_load(request_id)
    if not binding or binding.get("api_key_name") != key_name:
        return None
    channel = registry.get_channel(str(binding.get("channel_key") or ""))
    model = str(binding.get("model") or "")
    if (not isinstance(channel, XAIOAuthChannel)
            or not channel.supports_media_model("video", model)
            or (binding.get("state_key") and binding["state_key"] != channel.state_key)):
        return None
    cfg = media_config.settings("video")
    max_bytes = imagine._media_cache_file_limit(cfg)
    raw, mime = await imagine._download_xai_media(
        upstream_url, channel=channel, model=model, max_bytes=max_bytes,
    )
    cfg["_media_kind"] = "video"
    url, _expiry = await asyncio.to_thread(
        image_artifacts.publish,
        raw,
        mime=mime or "video/mp4",
        cfg=cfg,
        provider="xai",
        action="temporary",
        index=0,
        media_type="video",
        ttl_seconds=int(policy.settings().get("mediaTtlSeconds") or 3600),
        base_url=base_url,
    )
    return url


async def _run_video_status(arguments: dict, *, key_name: Optional[str],
                            base_url: str) -> tuple[dict, dict[str, Any]]:
    from ..xai import imagine

    request_id = str(arguments.get("request_id") or "").strip()
    if not request_id:
        raise ToolError("video_status 需要 request_id。")

    request = _synthesized_request({}, key_name=key_name, base_url=base_url,
                                   path="/v1/videos/" + request_id)
    response = await imagine.handle_video_result(request, request_id)
    body = _decode_json_response(response)
    if response.status_code >= 400:
        raise ToolError(_response_error_message(body))

    status = str(body.get("status") or "").strip()
    result: dict[str, Any] = {
        "request_id": request_id,
        "status": status,
        "progress": body.get("progress"),
        "model": body.get("model"),
    }
    # Query success does not mean generation success. Preserve the upstream
    # failure detail without changing the existing status-query isError semantics.
    if body.get("error") is not None:
        result["error"] = body["error"]
    video = body.get("video") if isinstance(body.get("video"), dict) else {}
    upstream_url = str(video.get("url") or "").strip()
    extra: dict[str, Any] = {"video_request_id": request_id, "model": body.get("model"),
                             "source_type": "xai"}
    if upstream_url and status.lower() in ("completed", "succeeded", "success", "done"):
        try:
            published = await _publish_video(
                request_id, upstream_url, key_name=key_name, base_url=base_url,
            )
        except Exception:
            published = None
        result["video_url"] = published or upstream_url
        result["delivery"] = "parrot_url" if published else "upstream_url"
        if not published:
            result["note"] = "未能转存为 Parrot 资源 URL，返回的是上游地址，可能很快过期。"
        extra["result_count"] = 1
        if published:
            extra["media_tokens"] = [image_artifacts.media_token(published)]
    elif upstream_url:
        result["video_url"] = upstream_url
    return result, extra


_HANDLERS = {
    "web_search": None,   # 搜索走异步函数，见 _dispatch
    "web_fetch": None,
}


async def _dispatch(tool_name: str, arguments: dict, *, key_name: Optional[str],
                    base_url: str, request_id: str) -> tuple[Any, dict[str, Any]]:
    """执行一个工具，返回 (结果, 日志附加字段)。"""
    if tool_name in ("web_search", "web_fetch"):
        result, extra = await _run_search(tool_name, arguments, request_id=request_id)
        return result, extra
    if tool_name in ("image_generate", "image_edit"):
        return await _run_image(tool_name, arguments, key_name=key_name, base_url=base_url)
    if tool_name == "video_generate":
        return await _run_video_create(arguments, key_name=key_name, base_url=base_url)
    if tool_name == "video_status":
        return await _run_video_status(arguments, key_name=key_name, base_url=base_url)
    raise ToolError(f"未知工具 {tool_name}。")


# ── 协议处理器 ──────────────────────────────────────────────────────────────


async def on_list_tools(ctx: ServerRequestContext, params: Any) -> types.ListToolsResult:
    """按该 Key 的实际授权生成工具表；每次调用都反映当前配置。"""
    key_name = _key_name(ctx)
    return types.ListToolsResult(
        tools=[build_tool(name, key_name) for name in policy.allowed_tools(key_name)]
    )


async def on_call_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams):
    """执行一次工具调用；限流、取消和日志均由本次调用拥有。"""
    tool_name = str(params.name or "")
    arguments = params.arguments if isinstance(params.arguments, dict) else {}
    key_name = _key_name(ctx)
    base_url = _base_url(ctx)
    client_name, client_version, protocol_version = _client_info(ctx)
    call_id = uuid.uuid4().hex
    started = time.monotonic()
    handle = None
    record_task = asyncio.create_task(asyncio.to_thread(
        log_db.record_mcp_call,
        call_id=call_id, tool_name=tool_name, api_key_name=key_name,
        client_name=client_name, client_version=client_version,
        protocol_version=protocol_version, params=arguments,
    ))

    async def finish(status: str, *, error_code: Optional[str] = None,
                     error_message: Optional[str] = None, extra: Optional[dict] = None) -> None:
        if handle is None:
            return
        payload = dict(extra or {})
        payload.setdefault("elapsed_ms", int((time.monotonic() - started) * 1000))
        try:
            await await_owned(asyncio.to_thread(
                log_db.finish_mcp_call, handle, status=status,
                error_code=error_code, error_message=error_message, **payload,
            ))
        except Exception:
            pass

    async def fail(message: str, *, status: str, error_code: str) -> types.CallToolResult:
        await finish(status, error_code=error_code, error_message=message)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=message)], is_error=True,
        )

    async def perform() -> types.CallToolResult:
        nonlocal handle
        try:
            # Retain the handle even if cancellation arrives while SQLite is writing.
            handle = await await_owned(record_task)
        except Exception:
            pass
        if tool_name not in catalog.TOOL_NAMES:
            return await fail(f"未知工具 {tool_name}。", status="denied", error_code="unknown_tool")
        if not policy.tool_allowed(key_name, tool_name):
            return await fail(policy.denial_reason(key_name, tool_name),
                              status="denied", error_code="tool_not_allowed")

        partial_error = None
        try:
            async with asyncio.timeout(_timeout_override(arguments)):
                # The SDK already consumed the body. Do not let a second receive
                # watcher compete with its transport; task cancellation drops the waiter.
                lease = await apikey_limiter.acquire(key_name)
                try:
                    entry = auth.api_key_entry(key_name)
                    if not entry or entry.get("enabled") is False or not policy.tool_allowed(key_name, tool_name):
                        return await fail("当前 API Key 或工具授权已失效。", status="denied",
                                          error_code="tool_not_allowed")
                    result, extra = await _dispatch(
                        tool_name, arguments, key_name=key_name, base_url=base_url, request_id=call_id,
                    )
                finally:
                    await await_owned(lease.release())
        except _PartialToolResult as exc:
            result, extra, partial_error = exc.result, exc.extra, str(exc)
        except apikey_limiter.ApiKeyLimitError as exc:
            return await fail(exc.message, status="denied", error_code="api_key_limit_" + exc.reason)
        except ToolError as exc:
            return await fail(str(exc), status="error", error_code="tool_error")
        except search_service.SearchError as exc:
            return await fail(f"{exc.message}（{exc.code}）", status="error", error_code=exc.code)
        except asyncio.TimeoutError:
            return await fail("调用超时，请重试或缩小请求范围。", status="timeout", error_code="timeout")
        except Exception:
            return await fail("工具执行失败，请稍后重试或联系服务管理员。",
                              status="error", error_code="internal_error")

        text = json.dumps(result, ensure_ascii=False, default=str)
        extra = dict(extra or {})
        extra.setdefault("result_bytes", len(text.encode("utf-8")))
        try:
            await await_owned(asyncio.to_thread(log_db.save_mcp_call_detail, handle, result))
        except Exception:
            pass
        await finish("error" if partial_error else "success", extra=extra,
                     error_code="partial_result" if partial_error else None,
                     error_message=partial_error)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            structured_content=result if isinstance(result, dict) else None,
            is_error=partial_error is not None,
        )

    try:
        return await perform()
    except asyncio.CancelledError:
        if handle is None and record_task.done() and not record_task.cancelled():
            try:
                handle = record_task.result()
            except Exception:
                pass
        try:
            # SDK shutdown uses AnyIO level cancellation; plain asyncio shielding
            # alone would re-raise at every checkpoint and leave a running row.
            with anyio.CancelScope(shield=True):
                await await_owned(finish("error", error_code="cancelled", error_message="工具调用已取消。"))
        finally:
            raise


def build_server() -> Server:
    """构造 MCP 服务端（每次 tools/list 都按当前配置与该 Key 计算）。"""
    from .. import __version__

    return Server(
        SERVER_NAME,
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
