"""下游 API Key 验证（常数时间比较，防止时序侧信道）。

返回三元组 (key_name, allowed_models, err)：
  - 验证通过：allowed_models 为列表（空 = 无限制，非空 = 白名单）
  - 验证失败：allowed_models 置空，err 为原因字符串
"""

import hmac
from typing import Optional

from . import config


def validate(headers) -> tuple[Optional[str], list[str], Optional[str]]:
    """验证请求头中的 API Key。

    headers: 类 dict，支持 `.get(key)`，key 大小写不敏感。

    返回:
      (key_name, allowed_models, None)  — 验证通过
      (None,     [],             err)   — 验证失败
    """
    auth_h = headers.get("authorization") or ""
    api_key = headers.get("x-api-key") or ""

    token = ""
    if auth_h.lower().startswith("bearer "):
        token = auth_h[7:].strip()
    elif api_key:
        token = api_key.strip()

    if not token:
        return None, [], "Missing API key"

    cfg = config.get()
    for name, entry in (cfg.get("apiKeys") or {}).items():
        if not isinstance(entry, dict):
            continue
        key_value = entry.get("key", "")
        if not key_value:
            continue
        if hmac.compare_digest(str(key_value), token):
            if entry.get("enabled") is False:
                return None, [], "API key is disabled"
            allowed = list(entry.get("allowedModels") or [])
            return name, allowed, None

    return None, [], "Invalid API key"


def images_allowed(key_name: Optional[str]) -> bool:
    """该 Key 是否允许调用 Parrot 图片生成/编辑接口。默认 False。"""
    if not key_name:
        return False
    cfg = config.get()
    entry = (cfg.get("apiKeys") or {}).get(key_name)
    if not isinstance(entry, dict):
        return False
    return bool(entry.get("allowImages", False))


def videos_allowed(key_name: Optional[str]) -> bool:
    """该 Key 是否允许调用 Parrot 视频生成/编辑接口。默认 False。"""
    if not key_name:
        return False
    cfg = config.get()
    entry = (cfg.get("apiKeys") or {}).get(key_name)
    if not isinstance(entry, dict):
        return False
    return bool(entry.get("allowVideos", False))


def get_allowed_protocols(key_name: Optional[str]) -> list[str]:
    """Deprecated compatibility shim.

    API Keys no longer gate Anthropic/OpenAI protocol entrances.  Route safety is
    decided by ProtocolMatrix + provider capabilities; model access remains
    controlled by ``allowedModels`` returned from ``validate()``.
    """
    return []
