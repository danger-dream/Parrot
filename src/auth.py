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
