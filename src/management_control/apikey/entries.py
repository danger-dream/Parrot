"""Stored API key entry normalization shared by control reads and mutations."""
from __future__ import annotations

from typing import Any


def normalize_entry(raw: Any) -> dict:
    if isinstance(raw, str):
        return {"key": raw, "enabled": True, "allowedModels": [], "allowImages": False,
                "allowVideos": False, "allowMcp": False, "mcpTools": []}
    if isinstance(raw, dict):
        # modelBlocks is opt-in: reads may expose [], but unrelated writes must
        # preserve absence and any explicitly configured (including empty) list.
        result = dict(raw)
        result.setdefault("enabled", True)
        result.setdefault("allowedModels", [])
        result.setdefault("allowImages", False)
        result.setdefault("allowVideos", False)
        # 既有条目没有 MCP 字段时必须保持关闭，不能因缺字段就放行。
        result.setdefault("allowMcp", False)
        result.setdefault("mcpTools", [])
        return result
    return {}


def clean_models(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for item in raw:
        value = str(item or "").strip()
        if value and value not in out:
            out.append(value)
    return tuple(out)

