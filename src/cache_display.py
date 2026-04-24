"""Token / prompt-cache display helpers.

统一 UI 展示口径：
  prompt_total = input + cache_creation + cache_read
  cache_rate   = cache_read / prompt_total

只展示读缓存；写缓存通常为 0，UI 层默认不展示，避免噪音。
本模块不依赖 telegram，供菜单与 oauth_manager 共用。
"""

from __future__ import annotations

from typing import Any


def _to_int(v: Any) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


def prompt_total(input_tokens: Any = 0, cache_creation: Any = 0, cache_read: Any = 0) -> int:
    """完整 prompt token 数。"""
    return _to_int(input_tokens) + _to_int(cache_creation) + _to_int(cache_read)


def prompt_total_from_row(row: dict, *, aggregate: bool = False) -> int:
    """从日志行/聚合行里读取完整 prompt。

    aggregate=False: request_log 行字段 input_tokens/cache_creation_tokens/cache_read_tokens
    aggregate=True : stats_summary 聚合字段 total_prompt_tokens 优先；否则 total_* 字段
    """
    if aggregate:
        explicit = row.get("total_prompt_tokens")
        if explicit is not None:
            return _to_int(explicit)
        return prompt_total(
            row.get("total_input_tokens"),
            row.get("total_cache_creation"),
            row.get("total_cache_read"),
        )
    return prompt_total(
        row.get("input_tokens"),
        row.get("cache_creation_tokens"),
        row.get("cache_read_tokens"),
    )


def fmt_tokens(n: Any) -> str:
    """1234567 → 1.2M；1234 → 1.2K；else → 原样。"""
    n = _to_int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def fmt_rate(num: Any, denom: Any) -> str:
    try:
        num_f = float(num or 0)
        denom_f = float(denom or 0)
    except Exception:
        return "N/A"
    if denom_f <= 0:
        return "N/A"
    return f"{num_f / denom_f * 100:.1f}%"


def cache_read_label(cache_read: Any, prompt: Any) -> str:
    """读缓存展示：`51.7K (60.8%)`。"""
    cr = _to_int(cache_read)
    pt = _to_int(prompt)
    return f"{fmt_tokens(cr)} ({fmt_rate(cr, pt)})"


def cache_read_phrase(cache_read: Any, prompt: Any, *, prefix: str = "缓存") -> str:
    """完整读缓存短语：`缓存 51.7K (60.8%)`。"""
    return f"{prefix} {cache_read_label(cache_read, prompt)}"


def cache_read_phrase_from_parts(input_tokens: Any, cache_creation: Any, cache_read: Any, *, prefix: str = "缓存") -> str:
    pt = prompt_total(input_tokens, cache_creation, cache_read)
    return cache_read_phrase(cache_read, pt, prefix=prefix)


def cache_read_phrase_from_row(row: dict, *, aggregate: bool = False, prefix: str = "缓存") -> str:
    """从日志行/聚合行输出 `缓存 51.7K (60.8%)`。"""
    prompt = prompt_total_from_row(row, aggregate=aggregate)
    if aggregate:
        cr = row.get("total_cache_read")
    else:
        cr = row.get("cache_read_tokens")
    return cache_read_phrase(cr, prompt, prefix=prefix)


def has_cache_read(cache_read: Any) -> bool:
    return _to_int(cache_read) > 0
