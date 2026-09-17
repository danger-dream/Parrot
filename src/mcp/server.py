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

import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.server import ServerRequestContext
from mcp.server.mcpserver.exceptions import ToolError
from starlette.responses import Response

from .. import auth, config, image_artifacts, log_db, search_service
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
    return {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词。"},
            "max_results": {
                "type": "integer", "minimum": 1, "maximum": 20,
                "description": "返回结果条数上限；省略则用服务端默认值。",
            },
            "freshness": {
                "type": "string", "enum": ["day", "week", "month", "year"],
                "description": "只返回该时间范围内发布的内容。",
            },
            "allowed_domains": {
                "type": "array", "items": {"type": "string"},
                "description": "只在这些域名内搜索。",
            },
            "blocked_domains": {
                "type": "array", "items": {"type": "string"},
                "description": "排除这些域名。",
            },
            **catalog.COMMON_PROPERTIES,
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
            **catalog.COMMON_PROPERTIES,
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
        "model": {"type": "string", "description": "指定图片模型；省略或 auto 表示自动选择。"},
        **catalog.COMMON_PROPERTIES,
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
            "model": {"type": "string", "description": "指定视频模型；省略或 auto 表示自动选择。"},
            **catalog.COMMON_PROPERTIES,
        },
        "required": ["prompt"],
    }


def _video_status_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "request_id": {"type": "string", "description": "video_generate 返回的任务 ID。"},
            **catalog.COMMON_PROPERTIES,
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


def build_tool(tool_name: str) -> types.Tool:
    """按当前配置构造一个工具定义（说明含实时可选值）。"""
    return types.Tool(
        name=tool_name,
        description=catalog.render_description(tool_name),
        input_schema=_SCHEMA_BUILDERS[tool_name](),
    )


# ── 工具实现 ────────────────────────────────────────────────────────────────


def _pick_source(arguments: dict) -> Optional[str]:
    """从统一参数里取出 source；auto/空 表示不指定。"""
    value = str(arguments.get("source") or "").strip()
    if not value or value.lower() == catalog.AUTO:
        return None
    return value


def _apply_source(arguments: dict, *, kind: str) -> Optional[str]:
    """校验并消费统一 source 参数，返回指定的来源 id。

    不可用时抛 ToolError 并带上当前可用列表，让模型能自行纠正（而不是逐个试）。
    """
    source = _pick_source(arguments)
    if source is None:
        return None
    if kind == "search":
        available = catalog.available_engines()
        if source not in available:
            raise ToolError(
                f"搜索引擎 {source!r} 当前不可用。当前可用：{', '.join(available) or '无'}。"
            )
        return source
    if kind == "image":
        available = catalog.image_sources()
    else:
        available = catalog.video_sources()
    if source not in available:
        raise ToolError(
            f"模型 {source!r} 当前不可用。当前可用：{', '.join(available) or '无'}。"
        )
    return source


def _timeout_override(arguments: dict) -> Optional[float]:
    value = arguments.get("timeout_seconds")
    if value is None:
        return None
    try:
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
                "language", "country"):
        if arguments.get(key) is not None:
            payload[key] = arguments[key]
    return {key: value for key, value in payload.items() if value is not None}


def _extract_arguments(arguments: dict) -> dict:
    payload: dict[str, Any] = {"url": arguments.get("url")}
    if arguments.get("max_chars") is not None:
        # search_service 用 maxFetchChars 作为正文上限；这里换算为等价参数。
        payload["max_chars"] = arguments["max_chars"]
    return payload


async def _run_search(tool_name: str, arguments: dict, *, request_id: str) -> dict:
    source = _apply_source(arguments, kind="search")
    if tool_name == "web_search":
        result = await search_service.search(
            _search_arguments(arguments), request_id=request_id,
            backend_id=source, origin="mcp",
        )
    else:
        result = await search_service.extract(
            _extract_arguments(arguments), request_id=request_id,
            backend_id=source, origin="mcp",
        )
    # 只把模型有权看到的字段交出去：来源标识、每次尝试遥测与计费事实留在服务端。
    from ..local_web_tools import _model_visible_search_result

    return _model_visible_search_result(result, "search" if tool_name == "web_search" else "extract")


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


async def _run_image(tool_name: str, arguments: dict, *, key_name: Optional[str],
                     base_url: str) -> tuple[dict, dict[str, Any]]:
    """调用既有图片处理器；返回 (模型可见结果, 日志附加字段)。"""
    from ..openai import images_openai_compat

    _apply_source(arguments, kind="image")
    payload: dict[str, Any] = {
        "model": str(arguments.get("model") or "").strip() or "auto",
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
    if response.status_code >= 400:
        raise ToolError(_response_error_message(body))

    data = body.get("data") if isinstance(body.get("data"), list) else []
    urls = [str(item.get("url")) for item in data if isinstance(item, dict) and item.get("url")]
    if not urls:
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
    return result, extra


async def _run_video_create(arguments: dict, *, key_name: Optional[str],
                            base_url: str) -> tuple[dict, dict[str, Any]]:
    from ..xai import imagine

    _apply_source(arguments, kind="video")
    payload: dict[str, Any] = {
        "model": str(arguments.get("model") or "").strip() or "auto",
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
        result = await _run_search(tool_name, arguments, request_id=request_id)
        extra: dict[str, Any] = {
            "source_id": result.get("backend_id"),
            "source_type": result.get("provider"),
            "result_count": len(result.get("results") or []) if isinstance(result.get("results"), list) else 0,
        }
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
        tools=[build_tool(name) for name in policy.allowed_tools(key_name)]
    )


async def on_call_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams):
    """执行一次工具调用，并把结果写入独立的 mcp_call_log。"""
    tool_name = str(params.name or "")
    arguments = params.arguments if isinstance(params.arguments, dict) else {}
    key_name = _key_name(ctx)
    base_url = _base_url(ctx)
    client_name, client_version, protocol_version = _client_info(ctx)
    call_id = uuid.uuid4().hex
    started = time.monotonic()

    handle = None
    try:
        handle = await asyncio.to_thread(
            log_db.record_mcp_call,
            call_id=call_id, tool_name=tool_name, api_key_name=key_name,
            client_name=client_name, client_version=client_version,
            protocol_version=protocol_version, params=arguments,
        )
    except Exception:
        handle = None

    async def finish(status: str, *, error_code: Optional[str] = None,
                     error_message: Optional[str] = None, extra: Optional[dict] = None) -> None:
        if handle is None:
            return
        payload = dict(extra or {})
        payload.setdefault("elapsed_ms", int((time.monotonic() - started) * 1000))
        try:
            await asyncio.to_thread(
                log_db.finish_mcp_call, handle, status=status,
                error_code=error_code, error_message=error_message, **payload,
            )
        except Exception:
            pass

    async def fail(message: str, *, status: str, error_code: str) -> types.CallToolResult:
        """把工具失败作为 is_error 结果返回，而不是协议级异常。

        MCP 的工具执行失败应当由模型看到并自行纠正；若抛成协议错误，
        多数客户端只会把它当成连接级失败，模型拿不到原因。
        """
        await finish(status, error_code=error_code, error_message=message)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=message)],
            is_error=True,
        )

    # 授权判定先于任何实际调用：未授权的工具不会触达上游。
    if tool_name not in catalog.TOOL_NAMES:
        return await fail(f"未知工具 {tool_name}。", status="denied", error_code="unknown_tool")
    if not policy.tool_allowed(key_name, tool_name):
        return await fail(policy.denial_reason(key_name, tool_name),
                          status="denied", error_code="tool_not_allowed")

    try:
        result, extra = await _dispatch(
            tool_name, arguments, key_name=key_name, base_url=base_url, request_id=call_id,
        )
    except ToolError as exc:
        return await fail(str(exc), status="error", error_code="tool_error")
    except search_service.SearchError as exc:
        return await fail(f"{exc.message}（{exc.code}）", status="error", error_code=exc.code)
    except asyncio.TimeoutError:
        return await fail("调用超时，请重试或缩小请求范围。", status="timeout", error_code="timeout")
    except Exception:
        # 异常文本可能带凭据或上游细节，只记录稳定描述，不把它回给模型。
        return await fail("工具执行失败，请稍后重试或联系服务管理员。",
                          status="error", error_code="internal_error")

    text = json.dumps(result, ensure_ascii=False, default=str)
    extra = dict(extra or {})
    if "result_bytes" not in extra:
        extra["result_bytes"] = len(text.encode("utf-8"))
    await finish("success", extra=extra)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structured_content=result if isinstance(result, dict) else None,
    )


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
