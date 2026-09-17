"""MCP 授权与实时配置判定。

判定顺序（逐层收窄，任何一层关闭都不可绕过）：

  1. ``mcp.enabled``           全局总开关
  2. ``mcp.tools.<tool>``      全局工具开关
  3. ``apiKeys.<name>.allowMcp``   该 Key 是否获准访问 MCP
  4. ``apiKeys.<name>.mcpTools``   该 Key 选用的工具（空 = 跟随全局）
  5. ``allowImages`` / ``allowVideos``  图片/视频工具额外的既有媒体权限

第 4 步只做交集，不能反向放宽第 2 步；第 5 步复用既有权限位，不新造一套。
"""
from __future__ import annotations

from typing import Any, Optional

from .. import auth, config
from .catalog import MEDIA_TOOL_REQUIREMENTS, TOOL_NAMES


def settings() -> dict[str, Any]:
    """生效的 MCP 配置（默认值 + 用户配置）。"""
    defaults = (config.DEFAULT_CONFIG.get("mcp") or {})
    current = config.get().get("mcp")
    result: dict[str, Any] = {key: value for key, value in defaults.items()}
    if isinstance(current, dict):
        for key, value in current.items():
            result[key] = value
    tools = dict(defaults.get("tools") or {})
    if isinstance(current, dict) and isinstance(current.get("tools"), dict):
        tools.update(current["tools"])
    # 未声明的工具名一律按默认（开启）处理，避免新增工具时被旧配置误关。
    result["tools"] = {name: bool(tools.get(name, True)) for name in TOOL_NAMES}
    return result


def enabled() -> bool:
    """全局总开关。"""
    return bool(settings().get("enabled", True))


def global_tool_enabled(tool_name: str) -> bool:
    """该工具是否被全局开启。未知工具名一律 False。"""
    if tool_name not in TOOL_NAMES:
        return False
    return bool(settings()["tools"].get(tool_name, True))


def tool_allowed(key_name: Optional[str], tool_name: str) -> bool:
    """综合判定：该 Key 此刻能否使用该工具。"""
    if not enabled() or not global_tool_enabled(tool_name):
        return False
    if not auth.mcp_allowed(key_name):
        return False
    selected = auth.mcp_selected_tools(key_name)
    if selected and tool_name not in selected:
        return False
    requirement = MEDIA_TOOL_REQUIREMENTS.get(tool_name)
    if requirement == "images" and not auth.images_allowed(key_name):
        return False
    if requirement == "videos" and not auth.videos_allowed(key_name):
        return False
    return True


def allowed_tools(key_name: Optional[str]) -> list[str]:
    """该 Key 在 ``tools/list`` 中能看到的工具，保持稳定顺序。"""
    return [name for name in TOOL_NAMES if tool_allowed(key_name, name)]


def denial_reason(key_name: Optional[str], tool_name: str) -> str:
    """给模型看的拒绝原因：写清是哪一层关掉了，便于用户自行纠正。"""
    if not enabled():
        return "MCP 服务当前已关闭，请联系服务管理员开启。"
    if not global_tool_enabled(tool_name):
        return f"工具 {tool_name} 已被服务端全局禁用，请改用其他工具或联系管理员。"
    if not auth.mcp_allowed(key_name):
        return "当前 API Key 未开启 MCP 访问权限。"
    selected = auth.mcp_selected_tools(key_name)
    if selected and tool_name not in selected:
        return (f"当前 API Key 未授权使用 {tool_name}。"
                f"已授权：{', '.join(selected)}。")
    requirement = MEDIA_TOOL_REQUIREMENTS.get(tool_name)
    if requirement == "images" and not auth.images_allowed(key_name):
        return "当前 API Key 未开启图片权限，无法使用该工具。"
    if requirement == "videos" and not auth.videos_allowed(key_name):
        return "当前 API Key 未开启视频权限，无法使用该工具。"
    return f"工具 {tool_name} 当前不可用。"
