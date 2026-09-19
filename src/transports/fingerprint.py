"""TLS/H2 指纹伪装 transport（curl_cffi backend）。

背景
----
默认 httpx client 暴露的是 Python/OpenSSL 的 JA3/JA4 + h2 指纹。Cloud Code /
Antigravity 家族的 Google 前端（GFE）按客户端能力画像区分 IDE 进程与脚本直连：
TLS 指纹非浏览器、ALPN 不含 h2、header 序非 Electron 的请求，即使协议层
拟真到位也会被单独归类。CLIProxyAPI / zerogravity 一类实现的共同结论是这层
必须换掉，Python 侧无法自己造——只能换 TLS 栈。

本模块把某个 channel 的出站会话换到 curl_cffi 的 Chrome/BoringSSL 画像上，
同时保持 ``httpx.AsyncClient`` 接口不变：流式、超时、header 序全部原样透传，
failover / SSE 解析一行不改。

设计边界
--------
- **fail-open**：curl_cffi 缺失时工厂返回 None，调用方退回默认 httpx client。
  不阻断主链路，只打一次告警。
- **per-channel**：只有 channel 显式声明 ``tls_fingerprint`` 时才会走到这里；
  其他渠道继续用共享连接池，零影响。
- **显式代理由 transport 承担**：httpx 在传入自定义 transport 时会忽略 client 级
  ``proxy`` 参数，若不管就会静默绕过代理、从本机直连出口。因此 legacy SOCKS5
  代理会透传给 curl_cffi session。new-proxy chain（connector 自建 client）路径
  不叠加指纹，出口语义优先。
- **trace extension 不模拟**：http_runtime 传入的 httpx trace callback 是
  httpcore 的细粒度事件钩子，curl_cffi 无对应物；跳过只会丢失 dispatch 计时
  字段，不影响请求正确性。
"""

from __future__ import annotations

import warnings
from typing import Any

import httpx

# curl_cffi 提供 BoringSSL impersonation（JA3/JA4/ALPN/header 序）。
# wheels 覆盖 linux manylinux（含 Docker python:3.11-slim），缺它时降级。
_IMPORT_ERROR: str | None = None
try:
    from curl_cffi.requests import AsyncSession  # type: ignore
except Exception as exc:  # pragma: no cover - 取决于部署环境
    AsyncSession = None  # type: ignore[assignment]
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def backend_available() -> bool:
    """curl_cffi 是否可加载。"""
    return AsyncSession is not None


def backend_error() -> str | None:
    """加载失败原因（无则 None）。"""
    return _IMPORT_ERROR


class _CurlResponseStream(httpx.AsyncByteStream):
    """把 curl_cffi 的流式响应包装成 httpx 可读的 byte stream。

    failover 侧的消费方式是 ``response.aiter_bytes()`` / ``aread()``，
    由 httpx.AsyncByteStream 的默认实现驱动，这里只需提供 ``__aiter__``
    与安全关闭。
    """

    def __init__(self, curl_response: Any):
        self._resp = curl_response

    async def __aiter__(self):
        async for chunk in self._resp.aiter_content():
            yield chunk

    async def aclose(self) -> None:
        close = getattr(self._resp, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass


class ImpersonatedTransport(httpx.AsyncBaseTransport):
    """把 httpx 请求改由 curl_cffi 发送的 transport。

    一次 client（= 一个 outbound attempt）对应一个 curl_cffi AsyncSession，
    随 ``client.aclose()`` 释放，避免跨 event loop 复用 session 的绑定问题。
    """

    def __init__(self, impersonation: str, proxy: str = ""):
        if not impersonation:
            raise ValueError("impersonation profile must be a non-empty string")
        self._impersonation = impersonation
        # 显式代理必须由本 transport 承担：httpx 在传入自定义 transport 时
        # 会忽略 client 级 ``proxy`` 参数，静默绕过代理会改变出口 IP。
        self._proxy = proxy
        self._session: Any | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if AsyncSession is None:
            raise RuntimeError(
                f"curl_cffi unavailable; TLS impersonation requires it ({_IMPORT_ERROR})"
            )
        if self._session is None:
            # 默认 profile 挂在 session 上；单请求也可以覆盖。
            kwargs: dict[str, Any] = {}
            if self._proxy:
                kwargs["proxies"] = {"http": self._proxy, "https": self._proxy}
            self._session = AsyncSession(impersonate=self._impersonation, timeout=None, **kwargs)

        content = await request.aread()
        timeout = (request.extensions or {}).get("timeout")

        resp = await self._session.request(
            request.method,
            str(request.url),
            headers=dict(request.headers),
            data=content if content else None,
            stream=True,
            timeout=timeout,
        )

        response_headers = _decoded_response_headers(resp)

        return httpx.Response(
            resp.status_code,
            headers=response_headers,
            stream=_CurlResponseStream(resp),
            request=request,
            extensions={"network_stream": True},
        )

    async def aclose(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass


_DECODED_HEADER_DROP = frozenset({"content-encoding", "content-length"})


def _decoded_response_headers(resp: Any) -> httpx.Headers:
    """透传 curl_cffi 响应头，剥掉描述已解压 body 的两个字段。

    libcurl 会按 ``accept_encoding``（浏览器画像里的 gzip/deflate/br）自动解压，
    但不会移除 ``Content-Encoding`` / ``Content-Length``。若原样交给 httpx，
    httpx 会按头部再解压一次（实测报
    ``DecodingError: incorrect header check``）。这里剥字段，保持 body 与头部
    一致；请求方向的指纹不受影响（只动响应侧）。
    """
    try:
        raw_items = list(resp.headers.multi_items())
    except Exception:
        raw_items = list(dict(resp.headers).items())
    return httpx.Headers(
        [(k, v) for k, v in raw_items if k.lower() not in _DECODED_HEADER_DROP]
    )


def impersonation_transport(impersonation: str, proxy: str = "") -> httpx.AsyncBaseTransport | None:
    """按 profile 名（如 ``chrome131``）构造 transport；不可用时返回 None。

    ``proxy`` 非空时由 transport 自身承担代理（httpx 在自定义 transport 下会
    忽略 client 级 proxy）。
    """
    if not impersonation:
        return None
    if AsyncSession is None:
        warnings.warn(
            "curl_cffi not installed; TLS fingerprint impersonation disabled",
            RuntimeWarning,
            stacklevel=2,
        )
        return None
    try:
        return ImpersonatedTransport(impersonation, proxy=proxy)
    except Exception:
        return None
