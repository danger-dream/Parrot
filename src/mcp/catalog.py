"""MCP 工具名单一真相源：名称、说明、权限要求、实时可选值。

工具说明保持简短（模型需要知道"能干什么"），细节放在参数 schema 里。
可选值（搜索引擎 / 图片模型 / 视频模型）在每次 ``tools/list`` 时按当前配置
实时计算并注入，因此上游增删来源后模型看到的就是最新的，不需要重启或重连。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# 六个工具名。apiKeys.<name>.mcpTools 与 mcp.tools 都只能使用这里的名字。
TOOL_NAMES: tuple[str, ...] = (
    "web_search",
    "web_fetch",
    "image_generate",
    "image_edit",
    "video_generate",
    "video_status",
)

# 需要额外媒体权限的工具（除 allowMcp 外还要对应 allowImages/allowVideos）。
MEDIA_TOOL_REQUIREMENTS: dict[str, str] = {
    "image_generate": "images",
    "image_edit": "images",
    "video_generate": "videos",
    "video_status": "videos",
}

# 三个通用可选参数，六个工具语义一致：
#   source           指定使用哪个来源/后端；auto = 按 Parrot 优先级自动选择
#   model            指定模型；auto = 按当前配置
#   timeout_seconds  覆盖本次超时（秒）
COMMON_PROPERTIES: dict[str, dict[str, Any]] = {
    "source": {
        "type": "string",
        "description": "指定来源（搜索引擎或图片/视频来源）。auto = 自动选择。",
    },
    "timeout_seconds": {
        "type": "number",
        "description": "本次调用超时秒数；省略则使用 Parrot 当前配置。",
    },
}

AUTO = "auto"


@dataclass(frozen=True)
class ToolSpec:
    """一个 MCP 工具的静态定义；可选值部分在渲染时补齐。"""

    name: str
    description: str
    # 需要哪类媒体权限（None = 只要有 allowMcp 即可）
    media: str | None = None
    # 该工具是否消费 / 生成媒体（决定日志字段与交付方式）
    kind: str = "text"

    def render(self, options: list[str], detail: str = "") -> str:
        """拼出最终说明：固定文案 + 当前可选值。"""
        text = self.description
        if options:
            text += f"\n当前可用：{', '.join(options)}。"
        if detail:
            text += f"\n{detail}"
        return text


SPECS: dict[str, ToolSpec] = {
    "web_search": ToolSpec(
        name="web_search",
        description="搜索互联网，返回标题、URL 和摘要。可用 source 指定搜索引擎，"
                    "freshness 限定时间范围，allowed_domains/blocked_domains 限定或排除站点。",
        kind="search",
    ),
    "web_fetch": ToolSpec(
        name="web_fetch",
        description="抓取一个已知 URL 的正文内容。仅支持公开 http(s) 地址，"
                    "不接受内网地址。",
        kind="search",
    ),
    "image_generate": ToolSpec(
        name="image_generate",
        description="按提示词生成图片，返回可访问的图片 URL。",
        media="images",
        kind="image",
    ),
    "image_edit": ToolSpec(
        name="image_edit",
        description="基于给定图片按提示词修改或重绘，可传多张参考图与遮罩。",
        media="images",
        kind="image",
    ),
    "video_generate": ToolSpec(
        name="video_generate",
        description="提交视频生成任务，返回 request_id；随后用 video_status 查询进度。",
        media="videos",
        kind="video",
    ),
    "video_status": ToolSpec(
        name="video_status",
        description="用 request_id 查询视频任务状态；完成后返回视频 URL。",
        media="videos",
        kind="video",
    ),
}


def _search_sources() -> tuple[list[str], str]:
    """当前可用的搜索引擎 id 列表与补充说明。未配置任何来源时返回空列表。"""
    from .. import search_service

    rows = search_service.backend_statuses()
    usable = [row for row in rows if row.get("available")]
    ids = [str(row["id"]) for row in usable]
    disabled = [str(row["id"]) for row in rows if not row.get("available")]
    detail = ""
    if disabled:
        detail = f"已配置但当前不可用：{', '.join(disabled)}。"
    return ids, detail


def image_sources() -> list[str]:
    """当前可用的图片模型（客户端可见名）。"""
    from .. import image_catalog

    try:
        return list(image_catalog.available_models())
    except Exception:
        return []


def video_sources() -> list[str]:
    """当前可用的视频模型。"""
    from ..xai import imagine

    try:
        return list(imagine.video_models())
    except Exception:
        return []


def live_options(tool_name: str) -> tuple[list[str], str]:
    """按当前配置计算一个工具的实时可选值与补充说明。

    不可用的来源仍然会被列出（并标注"不可用"），这样模型能理解为什么调用
    失败，而不是反复重试一个已下线的来源。
    """
    if tool_name in ("web_search", "web_fetch"):
        return _search_sources()
    if tool_name in ("image_generate", "image_edit"):
        return image_sources(), ""
    if tool_name in ("video_generate", "video_status"):
        return video_sources(), ""
    return [], ""


def available_engines() -> list[str]:
    """供工具处理器复用的可用搜索引擎 id（不抛异常）。"""
    return _search_sources()[0]


def render_description(tool_name: str) -> str:
    """工具的最终说明文本（含实时可选值）。"""
    spec = SPECS.get(tool_name)
    if spec is None:
        return ""
    options, detail = live_options(tool_name)
    return spec.render(options, detail)
