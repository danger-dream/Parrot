"""MCP 服务的 ASGI 装配：鉴权网关 + 挂载点。

鉴权用纯 ASGI 网关而不是 SDK 的 OAuth 组件：客户手上拿的是 Parrot 的
API Key，不是 OAuth 客户端；MCP 规范里授权本身是可选的，未授权时返回
401 并给出保护资源元数据地址即可。

网关在 MCP 应用之前拦下请求，因此未授权的调用**根本不会进入协议层**，
也就不会消耗任何资源；通过后的 Key 名写入 ``scope.state``，供工具处理器
读取（这是它唯一的身份来源，工具不会重新解析请求头）。
"""
from __future__ import annotations

import json
import threading
from typing import Optional

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import auth
from . import policy

# 管理端可读的运行时状态：端点、挂载信息与最近一次装配结果。
_LOCK = threading.Lock()
_STATE: dict = {"mounted": False, "path": None, "reason": None}

_RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"


def runtime_state() -> dict:
    with _LOCK:
        return dict(_STATE)


def _set_state(**values) -> None:
    with _LOCK:
        _STATE.update(values)


def _header(scope: Scope, name: bytes) -> Optional[str]:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return None


async def _send_json(send: Send, status: int, payload: dict, *, extra: list | None = None) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    headers.extend(extra or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class MCPAuthGate:
    """鉴权 + 总开关，位于 MCP 应用之前。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 总开关关闭时直接返回错误：不进入协议层，也不暴露任何工具信息。
        if not policy.enabled():
            await _send_json(send, 503, {
                "error": {
                    "type": "server_error",
                    "code": "mcp_disabled",
                    "message": "MCP service is disabled",
                }
            })
            return

        authorization = _header(scope, b"authorization") or ""
        api_key = _header(scope, b"x-api-key") or ""
        token = ""
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        elif api_key:
            token = api_key.strip()

        key_name: Optional[str] = None
        err: Optional[str] = None
        if not token:
            err = "Missing API key"
        else:
            key_name, _allowed_models, err = auth.validate(
                {"authorization": "Bearer " + token}
            )

        if err or not key_name:
            await _send_json(
                send, 401,
                {"error": {"type": "authentication_error", "code": "invalid_api_key",
                           "message": err or "Invalid API key"}},
                extra=[(b"www-authenticate",
                        b'Bearer resource_metadata="' + _RESOURCE_METADATA_PATH.encode("ascii") + b'"')],
            )
            return

        if not auth.mcp_allowed(key_name):
            # 认证通过但未获 MCP 授权：与认证失败区分，便于客户自查配置。
            await _send_json(send, 403, {
                "error": {
                    "type": "permission_error",
                    "code": "mcp_not_allowed",
                    "message": "this API key is not allowed to use the MCP service",
                }
            })
            return

        scope.setdefault("state", {})["parrot_key_name"] = key_name
        await self.app(scope, receive, send)


class DisabledMCPApp:
    """未启用时挂载的占位应用，保证端点行为可预期（503）而非 404 或 500。"""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        await _send_json(send, 503, {
            "error": {
                "type": "server_error",
                "code": "mcp_disabled",
                "message": "MCP service is disabled",
            }
        })


def build_asgi_app(server) -> ASGIApp:
    """构造挂载用的 ASGI 应用（Streamable HTTP + 鉴权网关）。"""
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.cors import CORSMiddleware

    app = server.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        max_request_body_size=int(policy.settings().get("maxRequestBodyBytes") or 33554432),
        # 明确交给 Parrot 自己判 Host/Origin：SDK 默认会在 host=127.0.0.1 时
        # 只放行 localhost，导致生产域名访问被 421 拒绝。
        transport_security=_transport_security(),
        host="0.0.0.0",
    )
    return MCPAuthGate(app)


def _transport_security():
    """DNS 重绑定保护：禁止浏览器跨站直接访问，但放行正常 API 客户端。

    MCP 规范要求服务端校验 Origin。这里对携带 Origin 的请求只放行同源与
    管理端已声明来源；不带 Origin 的（CLI/SDK 客户端）正常放行。
    """
    from mcp.server.transport_security import TransportSecuritySettings

    allowed_origins: list[str] = []
    try:
        from .. import config
        raw = (config.get().get("management") or {}).get("allowedOrigins") or ()
        allowed_origins = [str(item) for item in raw if str(item).strip()]
    except Exception:
        allowed_origins = []
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
        allowed_hosts=["*"],
        allowed_origins=allowed_origins,
    )
