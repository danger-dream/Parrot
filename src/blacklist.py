"""首包文本黑名单匹配。

配置结构（config.contentBlacklist）：
  {
    "default":   ["keyword1", ...],          # 对所有渠道生效
    "byChannel": {                            # 按渠道分组（name 或 channel_key）
      "智谱Coding Plan Max": ["policy_violation"],
      "api:智谱Coding Plan Max": ["another"]
    }
  }

匹配规则：任一关键词出现在首包文本中即视为命中。
"""

from __future__ import annotations

from typing import Optional

from . import config


def match(text_or_bytes, channel_key: str) -> Optional[str]:
    """返回命中的关键词，或 None。"""
    if isinstance(text_or_bytes, bytes):
        try:
            text = text_or_bytes.decode("utf-8", errors="replace")
        except Exception:
            return None
    else:
        text = text_or_bytes
    if not text:
        return None

    cfg = config.get()
    bl = cfg.get("contentBlacklist") or {}
    words: list[str] = list(bl.get("default") or [])

    by_ch = bl.get("byChannel") or {}
    # 允许用 channel_key（如 "api:xxx"）或裸 name（如 "xxx"）作为 key
    if channel_key in by_ch:
        words.extend(by_ch[channel_key])
    if ":" in channel_key:
        bare = channel_key.split(":", 1)[1]
        if bare in by_ch:
            words.extend(by_ch[bare])

    for w in words:
        if w and w in text:
            return w
    return None
