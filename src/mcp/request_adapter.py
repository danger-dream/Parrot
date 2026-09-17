"""使 MCP 工具能够复用既有 HTTP 处理器的请求适配层。

``images_runtime.execute`` 与 ``xai.imagine`` 的处理器接收的是
``starlette.requests.Request``，但它们只用到四个部位：``headers``、``json()``、
``form()`` 和 ``base_url``。这里合成一个等价的 Request，把 MCP 调用接进
**同一条**处理链路——同一套权限判定、同一套渠道选择、同一套日志与缓存，
而不是把逻辑复制一遍。

``base_url`` 由调用方给出（从 MCP 请求本身还原），因此工具返回的资源 URL
与 HTTP 入口完全一致，不需要任何域名配置。
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from starlette.requests import Request


def synthesize(
    payload: Mapping[str, Any],
    *,
    path: str,
    headers: Mapping[str, str],
    base_url: str = "",
) -> Request:
    """构造一个只读、单次消费的合成请求。

    payload 会被序列化为 JSON body；headers 需带上调用方自己的 Authorization，
    使既有处理器的鉴权按原样生效（不绕过任何权限判定）。
    """
    body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            # 处理器只会读一次 body；第二次调用必须是断开而不是空 body。
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    raw_headers: list[tuple[bytes, bytes]] = []
    for key, value in headers.items():
        raw_headers.append((str(key).lower().encode("latin-1"), str(value).encode("latin-1")))
    if not any(key == b"content-type" for key, _ in raw_headers):
        raw_headers.append((b"content-type", b"application/json"))

    base = _split_base_url(base_url)
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": base["scheme"],
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "server": (base["host"], base["port"]),
        # 客户端地址只用于日志展示；这里不带任何来源信息，避免伪造下游 IP。
        "client": (base["host"], 0),
    }
    return Request(scope, receive=receive)


def _split_base_url(base_url: str) -> dict[str, Any]:
    """从 MCP 请求还原出的 base_url 中取出 scheme / host / port。"""
    from urllib.parse import urlsplit

    value = str(base_url or "").strip()
    parsed = urlsplit(value) if value else None
    scheme = (parsed.scheme if parsed and parsed.scheme else "http") or "http"
    host = (parsed.hostname if parsed and parsed.hostname else "") or "localhost"
    port: Optional[int] = parsed.port if parsed else None
    if port is None:
        port = 443 if scheme == "https" else 80
    return {"scheme": scheme, "host": host, "port": port}
