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
#   source           指定使用哪个来源/上游；auto = 按 Parrot 优先级自动选择
#   timeout_seconds  覆盖本次超时（秒）
#
# source 的**可选值按当前配置实时计算**，因此这里只保留构造逻辑，静态常量会
# 让模型看到过期的来源列表。enum 与工具说明取自同一份 live_options，
# 两者不会出现"说明里有、enum 里没有"的不一致。
AUTO = "auto"
# Preferred public name. If a pre-existing real backend already owns it, the
# virtual source moves to an ID outside the legal backend-ID alphabet instead of
# stealing that configuration on upgrade.
X_SEARCH_SOURCE = "x-twitter"
X_SEARCH_SOURCE_COMPAT = "x:twitter"


def x_search_source_id(rows: list[dict[str, Any]] | None = None) -> str:
    """Return the non-conflicting live ID for the virtual native X source."""
    if rows is None:
        from .. import search_service
        rows = search_service.backend_statuses()
    occupied = {str(row.get("id") or "") for row in rows}
    if X_SEARCH_SOURCE not in occupied:
        return X_SEARCH_SOURCE
    candidate = X_SEARCH_SOURCE_COMPAT
    while candidate in occupied:
        candidate += ":native"
    return candidate


def is_x_search_source_id(value: str, rows: list[dict[str, Any]] | None = None) -> bool:
    return bool(value) and value == x_search_source_id(rows)

_SOURCE_KIND_LABELS: dict[str, str] = {
    "search": "搜索引擎",
    "image": "图片模型",
    "video": "视频模型",
}


def source_kind(tool_name: str) -> str:
    """该工具的 source 指向哪一类上游。"""
    if tool_name in ("web_search", "web_fetch"):
        return "search"
    if tool_name in ("image_generate", "image_edit"):
        return "image"
    return "video"


# video_status 只按 request_id 查询已提交的任务，无法在选择上游上有意义；
# 给它一个 source 参数会误导模型以为能指定模型。
_NO_SOURCE_TOOLS = frozenset({"video_status"})


def accepts_source(tool_name: str) -> bool:
    return tool_name not in _NO_SOURCE_TOOLS


def source_property(tool_name: str, key_name: str | None = None) -> dict[str, Any]:
    """按当前配置生成 source 参数定义。

    带 enum 才能让客户端在参数层就约束取值；此前只有说明文字，模型容易填错。
    enum 始终包含 auto（等于"不指定，按优先级自动选择"）。没有可用来源时
    只留 auto，而不是给一个空 enum——空 enum 在多数客户端里是不可选的意思。
    """
    kind = source_kind(tool_name)
    options = live_options(tool_name, key_name)[0]
    description = f"指定{_SOURCE_KIND_LABELS[kind]}；省略或 auto = 按 Parrot 当前配置自动选择。"
    if tool_name == "web_search":
        x_source = x_search_source_id()
        description += (
            f" source={x_source} 使用 Grok 原生 X Search 搜索 X（Twitter）的帖子、用户和线程；"
            "真实 xAI backend source 仍使用 Grok Web Search 搜索普通网页。"
        )
        if x_source != X_SEARCH_SOURCE:
            description += (
                f" 由于 {X_SEARCH_SOURCE} 已被既有真实搜索后端占用，原生 X Search 使用"
                f"兼容 ID {x_source}，不会抢占原后端。"
            )
    prop: dict[str, Any] = {"type": "string", "description": description}
    prop["enum"] = [AUTO, *options]
    return prop


def common_properties(tool_name: str) -> dict[str, dict[str, Any]]:
    """按工具生成通用参数（source 带实时 enum + timeout_seconds）。"""
    properties: dict[str, dict[str, Any]] = {}
    if accepts_source(tool_name):
        properties["source"] = source_property(tool_name)
    properties["timeout_seconds"] = {
        "type": "number", "minimum": 0.1, "maximum": 600,
        "description": "本次调用超时秒数（含排队）；省略则使用 Parrot 当前配置。",
    }
    return properties


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
        description=(
            "搜索互联网或 X（Twitter），返回标题、URL 和摘要。可用 source 指定搜索来源；"
            "原生 X Search 来源见 source 参数的实时枚举，支持账号和日期过滤，但不支持"
            " allowed_domains/blocked_domains。其他 source 执行普通网页搜索。"
        ),
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
        description="按提示词生成图片，返回可访问的图片 URL。可用 source 指定图片模型。",
        media="images",
        kind="image",
    ),
    "image_edit": ToolSpec(
        name="image_edit",
        description="基于给定图片按提示词修改或重绘，可传多张参考图与遮罩。可用 source 指定图片模型。",
        media="images",
        kind="image",
    ),
    "video_generate": ToolSpec(
        name="video_generate",
        description="提交视频生成任务，返回 request_id；随后用 video_status 查询进度。可用 source 指定视频模型。",
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


def _search_sources(tool_name: str = "web_search") -> tuple[list[str], str]:
    """当前可用的搜索来源 id 列表与补充说明。"""
    from .. import search_service

    rows = search_service.backend_statuses()
    if tool_name == "web_fetch":
        # Match search_service's extract capability, not just credential readiness.
        rows = [row for row in rows if row.get("type") in ("anysearch", "tavily", "exa", "openai")]
    usable = [row for row in rows if row.get("available")]
    ids = [str(row["id"]) for row in usable]
    disabled = [str(row["id"]) for row in rows if not row.get("available")]
    if tool_name == "web_search":
        x_source = x_search_source_id(rows)
        xai_rows = [row for row in rows if row.get("type") == "xai"]
        if any(row.get("available") for row in xai_rows):
            # The virtual source is backed by every eligible xAI backend/account.
            # A real backend always keeps its ID and ordinary Web Search semantics.
            insertion = max((idx + 1 for idx, row in enumerate(usable)
                             if row.get("type") == "xai"), default=len(ids))
            ids.insert(insertion, x_source)
        elif x_source not in disabled:
            disabled.append(x_source)
    detail = ""
    if disabled:
        detail = f"当前不可用：{', '.join(disabled)}。"
        x_source = x_search_source_id(rows)
        if x_source in disabled:
            detail += f" {x_source} 需要至少一个已接入、启用且状态可用的 xAI OAuth 搜索账户。"
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
    from .. import image_catalog, model_state
    from ..xai import imagine

    try:
        available = {source.model for source in image_catalog.sources(kind="video")
                     if source.available and model_state.is_discovery_visible(source.model)}
        # Filtering must not change the configured fallback priority.
        return [model for model in imagine.video_models() if model in available]
    except Exception:
        return []


def media_sources(kind: str, key_name: str | None = None) -> list[str]:
    """Available models narrowed with the same whitelist/alias rules as HTTP.

    No key is used by configuration/catalog callers; actual MCP calls always
    supply their authenticated key. Downstream HTTP authorization remains final.
    """
    from .. import auth, model_mapping, model_names, model_state

    options = image_sources() if kind == "image" else video_sources()
    if key_name is None:
        return options
    entry = auth.api_key_entry(key_name)
    if not entry or entry.get("enabled") is False:
        return []
    allowed = set(model_names.expand_legacy_permissions(list(entry.get("allowedModels") or [])))
    if not allowed:
        return options
    mapping = model_mapping.get_global_map()
    # A key may grant only a global alias. Keep that authorized request name,
    # rather than replacing it with a real name the key has not been granted.
    options = list(options)
    available = set(options)
    options.extend(alias for alias, real in mapping.items()
                   if alias in allowed and alias not in available and real in available
                   and model_state.is_discovery_visible(alias)
                   and model_state.is_discovery_visible(real))
    return [model for model in options
            if model in allowed or mapping.get(model, model) in allowed]


def live_options(tool_name: str, key_name: str | None = None) -> tuple[list[str], str]:
    """按当前配置计算一个工具的实时可选值与补充说明。

    不可用的来源仍然会被列出（并标注"不可用"），这样模型能理解为什么调用
    失败，而不是反复重试一个已下线的来源。不接受 source 的工具返回空列表，
    避免说明里列出它其实用不上的可选值。
    """
    if not accepts_source(tool_name):
        return [], ""
    if tool_name in ("web_search", "web_fetch"):
        return _search_sources(tool_name)
    if tool_name in ("image_generate", "image_edit"):
        return media_sources("image", key_name), ""
    if tool_name in ("video_generate", "video_status"):
        return media_sources("video", key_name), ""
    return [], ""


def available_engines(tool_name: str = "web_search") -> list[str]:
    """供工具处理器复用的可用且支持对应操作的搜索引擎 id。"""
    return _search_sources(tool_name)[0]


def render_description(tool_name: str, key_name: str | None = None) -> str:
    """工具的最终说明文本（含实时可选值）。"""
    spec = SPECS.get(tool_name)
    if spec is None:
        return ""
    options, detail = live_options(tool_name, key_name)
    return spec.render(options, detail)
