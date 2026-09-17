"""Parrot 内建 MCP 服务：对授权客户暴露搜索 / 图片 / 视频工具。

模块边界：
  - ``catalog``          工具名单一真相源（名称、说明、权限要求、参数 schema）
  - ``policy``           全局开关 + Key 级授权的合并判定
  - ``request_adapter``  让 MCP 复用既有 HTTP 处理器的请求适配层
  - ``server``           MCP 协议服务端装配与工具处理器
  - ``mount``            ASGI 鉴权网关与挂载

本包不复制任何上游能力：搜索走 ``search_service``，图片走 ``images_runtime``，
视频走 ``xai.imagine``，与 HTTP 入口共用同一实现与同一套开关。
"""
from __future__ import annotations

from . import catalog, mount, policy, request_adapter, server

# MCP 端点路径。媒体资源端点在 server.py 里注册，使用同一个前缀。
PATH = "/mcp"
MEDIA_PATH = "/v1/mcp/media"

__all__ = [
    "PATH",
    "MEDIA_PATH",
    "catalog",
    "mount",
    "policy",
    "request_adapter",
    "server",
]
