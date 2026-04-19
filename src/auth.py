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
            allowed = list(entry.get("allowedModels") or [])
            return name, allowed, None

    return None, [], "Invalid API key"


def get_allowed_protocols(key_name: Optional[str]) -> list[str]:
    """返回该 Key 的 allowedProtocols 列表；空/未设 = 无限制（对所有入口开放）。

    合法值：`"anthropic"` / `"chat"` / `"responses"`。
    本函数只读 config，不做校验；写入路径（TG 菜单）保证字段正确。
    """
    if not key_name:
        return []
    cfg = config.get()
    entry = (cfg.get("apiKeys") or {}).get(key_name)
    if not isinstance(entry, dict):
        return []
    return list(entry.get("allowedProtocols") or [])
