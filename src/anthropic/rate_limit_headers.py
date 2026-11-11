"""Anthropic 响应头 rate-limit 解析器（被动采样与 429 统一信号提取）。

根据 Anthropic 实际响应头做被动采样，并提供窗口超限判断与统一限流信号提取。

响应头契约（CC v2.1.321）：
  - anthropic-ratelimit-unified-5h-utilization      0..1 小数
  - anthropic-ratelimit-unified-7d-utilization      0..1 小数
  - anthropic-ratelimit-unified-7d_oi-utilization   0..1 小数（seven_day_overage_included / Fable 周子限额）
  - anthropic-ratelimit-unified-{5h,7d,7d_oi}-reset Unix 秒（兼容毫秒：值 >1e11 自动 / 1000）
  - anthropic-ratelimit-unified-{5h,7d,7d_oi}-surpassed-threshold   "true" / "false"
  - anthropic-ratelimit-unified-{5h,7d,7d_oi}-status "allowed" / "allowed_warning" / "rejected"
  - anthropic-ratelimit-unified-status              "allowed" / "allowed_warning" / "rejected"
  - anthropic-ratelimit-unified-reset               当前代表性触发窗口的 Unix 秒时间戳
  - anthropic-ratelimit-unified-representative-claim 触发窗口名（five_hour / seven_day / seven_day_overage_included / ...）
  - anthropic-ratelimit-unified-overage-status      超额计费状态
  - anthropic-ratelimit-unified-overage-disabled-reason 超额不可用原因

关键单位陷阱：**响应头单位 0..1（小数），与 `/api/oauth/usage` JSON body 的
0..100 百分比单位不同**。Parrot 明确隔离两种数据源：被动采样只写
5h / 7d / 7d_oi(fable)（转成 0..100 存到 oauth_quota_cache 的公共列），
不触碰 sonnet/opus/extra（那些只有主动拉 /api/oauth/usage 才有）。

⚠ 严格不要把响应头采样结果当作"全量 usage"使用，它缺 sonnet/opus/extra 维度。
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any, Mapping


# ─── 单位换算 ────────────────────────────────────────────────────

def _parse_util_fraction(raw: Any) -> float | None:
    """'0.05' → 0.05；保留正常 >1 超额值，拒绝负数和非有限值。"""
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    # The public patch stores percentages. Reject overflow there too rather
    # than persisting an infinite utilization from an otherwise finite fraction.
    if value < 0 or not math.isfinite(value) or not math.isfinite(value * 100.0):
        return None
    return value


def _parse_reset_epoch_s(raw: Any) -> int | None:
    """Unix 秒（兼容毫秒自动除以 1000）→ 合理范围内的秒级时间戳。"""
    if raw is None or raw == "":
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(val):
        return None
    try:
        ts = int(val)
    except (OverflowError, ValueError):
        return None
    if ts > 1_000_000_000_000:   # >1e12 明确是毫秒
        ts //= 1000
    elif ts > 100_000_000_000:   # >1e11 大概率是毫秒（年份 >5000 秒时间戳才会到这范围）
        ts //= 1000
    # 合理范围校验（避免解析到奇怪值：2001 ~ 2096）
    if ts < 1_000_000_000 or ts > 4_000_000_000:
        return None
    return ts


def _parse_reset_iso(raw: Any) -> str | None:
    """Unix 秒（兼容毫秒自动除以 1000）→ ISO8601 UTC 字符串。"""
    ts = _parse_reset_epoch_s(raw)
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OSError, OverflowError, ValueError):
        return None


# ─── 主入口：解析响应头 → 分段 patch dict ─────────────────────────

# 响应头 key 必须都小写（httpx.Headers / dict(resp.headers) 都是小写）
H_5H_UTIL = "anthropic-ratelimit-unified-5h-utilization"
H_7D_UTIL = "anthropic-ratelimit-unified-7d-utilization"
H_7D_OI_UTIL = "anthropic-ratelimit-unified-7d_oi-utilization"
H_5H_RESET = "anthropic-ratelimit-unified-5h-reset"
H_7D_RESET = "anthropic-ratelimit-unified-7d-reset"
H_7D_OI_RESET = "anthropic-ratelimit-unified-7d_oi-reset"
H_5H_STATUS = "anthropic-ratelimit-unified-5h-status"
H_7D_STATUS = "anthropic-ratelimit-unified-7d-status"
H_7D_OI_STATUS = "anthropic-ratelimit-unified-7d_oi-status"
H_5H_SURPASS = "anthropic-ratelimit-unified-5h-surpassed-threshold"
H_7D_SURPASS = "anthropic-ratelimit-unified-7d-surpassed-threshold"
H_7D_OI_SURPASS = "anthropic-ratelimit-unified-7d_oi-surpassed-threshold"

H_UNIFIED_STATUS = "anthropic-ratelimit-unified-status"
H_UNIFIED_RESET = "anthropic-ratelimit-unified-reset"
H_REPRESENTATIVE_CLAIM = "anthropic-ratelimit-unified-representative-claim"
H_OVERAGE_STATUS = "anthropic-ratelimit-unified-overage-status"
H_OVERAGE_RESET = "anthropic-ratelimit-unified-overage-reset"
H_OVERAGE_DISABLED_REASON = "anthropic-ratelimit-unified-overage-disabled-reason"
H_OVERAGE_IN_USE = "anthropic-ratelimit-unified-overage-in-use"

_SUPPORTED_WINDOWS = ("5h", "7d", "7d_oi")


def _get_ci(headers: Mapping[str, Any] | None, key: str) -> Any:
    """大小写不敏感取头值（兼容 httpx.Headers / dict / lower-case dict）。"""
    if not headers:
        return None
    if hasattr(headers, "get"):
        v = headers.get(key)
        if v is not None:
            return v
        v = headers.get(key.lower())
        if v is not None:
            return v
        # 遍历兜底
        for k, val in (headers.items() if hasattr(headers, "items") else []):
            if str(k).lower() == key.lower():
                return val
    return None


def _clean_str(raw: Any, *, lower: bool = False) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return text.lower() if lower else text


def parse_rate_limit_headers(headers: Mapping[str, Any] | None) -> dict | None:
    """从响应头提取 5h/7d/7d_oi 段 usage 字段，返回 patch dict 或 None（无可用字段）。

    返回 dict 仅包含 state_db.oauth_quota_cache 的**子集**：
      five_hour_util / five_hour_reset / seven_day_util / seven_day_reset /
      fable_util / fable_reset
    sonnet/opus/extra 字段**不会**出现（响应头没这些维度）。

    注意：util 字段输出为 **0..100 百分比**（用 0..1 原始值 × 100 转换），
    与主动拉 `/api/oauth/usage` 的 JSON body（已是 0..100）对齐。
    """
    if not headers:
        return None

    fh_raw = _parse_util_fraction(_get_ci(headers, H_5H_UTIL))
    sd_raw = _parse_util_fraction(_get_ci(headers, H_7D_UTIL))
    foi_raw = _parse_util_fraction(_get_ci(headers, H_7D_OI_UTIL))
    fh_reset = _parse_reset_iso(_get_ci(headers, H_5H_RESET))
    sd_reset = _parse_reset_iso(_get_ci(headers, H_7D_RESET))
    foi_reset = _parse_reset_iso(_get_ci(headers, H_7D_OI_RESET))

    # 任一 util 或 reset 可用就算命中（部分响应可能只带其中一个窗口）
    if (
        fh_raw is None
        and sd_raw is None
        and foi_raw is None
        and fh_reset is None
        and sd_reset is None
        and foi_reset is None
    ):
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
    if foi_raw is not None:
        patch["fable_util"] = foi_raw * 100.0
    if foi_reset is not None:
        patch["fable_reset"] = foi_reset
    return patch


# ─── 超限检测与统一限流信号提取 ───────────────────────────────────

def is_window_exceeded(headers: Mapping[str, Any] | None, window: str) -> bool:
    """给定 window='5h'、'7d' 或 '7d_oi'，判断该窗口是否已触发 rate limit。

    顺序：surpassed-threshold=true > utilization >= 1.0（带浮点容差） > status=rejected。
    无相关头时返回 False（保守：不强行禁用）。
    """
    if window not in _SUPPORTED_WINDOWS:
        raise ValueError(f"window must be '5h', '7d', or '7d_oi', got {window!r}")
    if not headers:
        return False
    surpass_key = f"anthropic-ratelimit-unified-{window}-surpassed-threshold"
    util_key = f"anthropic-ratelimit-unified-{window}-utilization"
    status_key = f"anthropic-ratelimit-unified-{window}-status"

    st = _get_ci(headers, surpass_key)
    if st is not None and str(st).strip().lower() == "true":
        return True

    util = _parse_util_fraction(_get_ci(headers, util_key))
    if util is not None and util >= 1.0 - 1e-9:
        return True

    status = _clean_str(_get_ci(headers, status_key), lower=True)
    if status == "rejected":
        return True
    return False


def parse_unified_limit_signal(headers: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """提取 Anthropic 统一限流响应头（429 归因、重置时间与 overage 状态）。"""
    if not headers:
        return None
    status = _clean_str(_get_ci(headers, H_UNIFIED_STATUS), lower=True)
    reset_raw = _get_ci(headers, H_UNIFIED_RESET)
    reset_epoch_s = _parse_reset_epoch_s(reset_raw)
    reset_iso = _parse_reset_iso(reset_raw)
    representative_claim = _clean_str(_get_ci(headers, H_REPRESENTATIVE_CLAIM), lower=True)
    overage_status = _clean_str(_get_ci(headers, H_OVERAGE_STATUS), lower=True)
    overage_reset_raw = _get_ci(headers, H_OVERAGE_RESET)
    overage_reset_epoch_s = _parse_reset_epoch_s(overage_reset_raw)
    overage_reset_iso = _parse_reset_iso(overage_reset_raw)
    overage_disabled_reason = _clean_str(_get_ci(headers, H_OVERAGE_DISABLED_REASON), lower=False)
    overage_in_use_raw = _clean_str(_get_ci(headers, H_OVERAGE_IN_USE), lower=True)
    overage_in_use = (
        True if overage_in_use_raw == "true"
        else (False if overage_in_use_raw == "false" else None)
    )

    if (
        status is None
        and reset_epoch_s is None
        and representative_claim is None
        and overage_status is None
        and overage_reset_epoch_s is None
        and overage_disabled_reason is None
        and overage_in_use is None
    ):
        return None

    is_using_overage = bool(
        (status == "rejected" and overage_status in {"allowed", "allowed_warning"})
        or overage_in_use is True
    )
    return {
        "status": status,
        "reset_epoch_s": reset_epoch_s,
        "reset_iso": reset_iso,
        "representative_claim": representative_claim,
        "overage_status": overage_status,
        "overage_reset_epoch_s": overage_reset_epoch_s,
        "overage_reset_iso": overage_reset_iso,
        "overage_disabled_reason": overage_disabled_reason,
        "overage_in_use": overage_in_use,
        "is_using_overage": is_using_overage,
    }


def five_hour_status(headers: Mapping[str, Any] | None) -> str | None:
    """取 5h-status 原值（"allowed" / "allowed_warning" / ...）。"""
    return _clean_str(_get_ci(headers, H_5H_STATUS), lower=False)


def now_ms() -> int:
    return int(time.time() * 1000)
