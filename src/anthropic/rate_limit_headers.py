"""Anthropic 响应头 rate-limit 解析器（被动采样）。

参考 sub2api `backend/internal/service/ratelimit_service.go::UpdateSessionWindow`
的被动采样实现 + `calculateAnthropic429ResetTime` / `isAnthropicWindowExceeded`。

响应头契约（来自 sub2api 注释 + 实际抓包）：
  - anthropic-ratelimit-unified-5h-utilization     0..1 小数
  - anthropic-ratelimit-unified-7d-utilization     0..1 小数
  - anthropic-ratelimit-unified-{5h,7d}-reset      Unix 秒（兼容毫秒：值 >1e11 自动 / 1000）
  - anthropic-ratelimit-unified-{5h,7d}-surpassed-threshold   "true" / "false"
  - anthropic-ratelimit-unified-5h-status          "allowed" / "allowed_warning" / ...

关键单位陷阱：**响应头单位 0..1（小数），与 `/api/oauth/usage` JSON body 的
0..100 百分比单位不同**。sub2api 内部也是两套独立存储（UsageInfo vs account.Extra），
Parrot 对齐这个边界——被动采样写 5h/7d（转成 0..100 存到 oauth_quota_cache 的
公共列），不触碰 sonnet/opus/extra（那些只有主动拉 /api/oauth/usage 才有）。

⚠ 严格不要把响应头采样结果当作"全量 usage"使用，它缺 sonnet/opus/extra 维度。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Mapping


# ─── 单位换算 ────────────────────────────────────────────────────

def _parse_util_fraction(raw: Any) -> float | None:
    """'0.05' → 0.05；非法值返回 None。值保持 0..1 小数原样，不做 × 100。"""
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _parse_reset_iso(raw: Any) -> str | None:
    """Unix 秒（兼容毫秒自动除以 1000）→ ISO8601 UTC 字符串。

    sub2api `UpdateSessionWindow` line 1118 对 >1e11 的值自动识别为毫秒时间戳。
    """
    if raw is None or raw == "":
        return None
    try:
        ts = int(float(raw))
    except (TypeError, ValueError):
        return None
    if ts > 1_000_000_000_000:   # >1e12 明确是毫秒
        ts //= 1000
    elif ts > 100_000_000_000:   # >1e11 大概率是毫秒（年份 >5000 秒时间戳才会到这范围）
        ts //= 1000
    # 合理范围校验（避免解析到奇怪值）
    if ts < 1_000_000_000 or ts > 4_000_000_000:   # 2001 ~ 2096
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OSError, OverflowError, ValueError):
        return None


# ─── 主入口：解析响应头 → 分段 patch dict ─────────────────────────

# 响应头 key 必须都小写（httpx.Headers / dict(resp.headers) 都是小写）
H_5H_UTIL = "anthropic-ratelimit-unified-5h-utilization"
H_7D_UTIL = "anthropic-ratelimit-unified-7d-utilization"
H_5H_RESET = "anthropic-ratelimit-unified-5h-reset"
H_7D_RESET = "anthropic-ratelimit-unified-7d-reset"
H_5H_STATUS = "anthropic-ratelimit-unified-5h-status"
H_5H_SURPASS = "anthropic-ratelimit-unified-5h-surpassed-threshold"
H_7D_SURPASS = "anthropic-ratelimit-unified-7d-surpassed-threshold"


def _get_ci(headers: Mapping[str, Any], key: str) -> Any:
    """大小写不敏感取头值（兼容 httpx.Headers / dict / lower-case dict）。"""
    if hasattr(headers, "get"):
        v = headers.get(key)
        if v is not None:
            return v
        v = headers.get(key.lower())
        if v is not None:
            return v
        # 遍历兜底
        for k, val in (headers.items() if hasattr(headers, "items") else []):
            if k.lower() == key.lower():
                return val
    return None


def parse_rate_limit_headers(headers: Mapping[str, Any]) -> dict | None:
    """从响应头提取 5h/7d 段 usage 字段，返回 patch dict 或 None（无可用字段）。

    返回 dict 仅包含 state_db.oauth_quota_cache 的**子集**：
      five_hour_util / five_hour_reset / seven_day_util / seven_day_reset
    sonnet/opus/extra 字段**不会**出现（响应头没这些维度）。

    注意：util 字段输出为 **0..100 百分比**（用 0..1 原始值 × 100 转换），
    与主动拉 `/api/oauth/usage` 的 JSON body（已是 0..100）对齐，存到
    oauth_quota_cache 的 five_hour_util / seven_day_util 列后，UI 渲染层
    可以无差别使用。
    """
    if not headers:
        return None

    fh_raw = _parse_util_fraction(_get_ci(headers, H_5H_UTIL))
    sd_raw = _parse_util_fraction(_get_ci(headers, H_7D_UTIL))
    fh_reset = _parse_reset_iso(_get_ci(headers, H_5H_RESET))
    sd_reset = _parse_reset_iso(_get_ci(headers, H_7D_RESET))

    # 任一 util 或 reset 可用就算命中（部分响应可能只带其中一个窗口）
    if fh_raw is None and sd_raw is None and fh_reset is None and sd_reset is None:
        return None

    patch: dict = {}
    if fh_raw is not None:
        patch["five_hour_util"] = fh_raw * 100.0
    if fh_reset is not None:
        patch["five_hour_reset"] = fh_reset
    if sd_raw is not None:
        patch["seven_day_util"] = sd_raw * 100.0
    if sd_reset is not None:
        patch["seven_day_reset"] = sd_reset
    return patch


# ─── 超限检测（sub2api isAnthropicWindowExceeded 对齐） ──────────

def is_window_exceeded(headers: Mapping[str, Any], window: str) -> bool:
    """给定 window='5h' 或 '7d'，判断该窗口是否已触发 rate limit。

    顺序：surpassed-threshold=true > utilization >= 1.0（带浮点容差）。
    无相关头时返回 False（保守：不强行禁用）。
    """
    if window not in ("5h", "7d"):
        raise ValueError(f"window must be '5h' or '7d', got {window!r}")
    surpass_key = f"anthropic-ratelimit-unified-{window}-surpassed-threshold"
    util_key = f"anthropic-ratelimit-unified-{window}-utilization"

    st = _get_ci(headers, surpass_key)
    if st is not None and str(st).strip().lower() == "true":
        return True

    util = _parse_util_fraction(_get_ci(headers, util_key))
    if util is not None and util >= 1.0 - 1e-9:
        return True
    return False


def five_hour_status(headers: Mapping[str, Any]) -> str | None:
    """取 5h-status 原值（"allowed" / "allowed_warning" / ...）。"""
    v = _get_ci(headers, H_5H_STATUS)
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def now_ms() -> int:
    return int(time.time() * 1000)
