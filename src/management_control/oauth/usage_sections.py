"""Structured OAuth usage sections: everything Telegram's account detail shows.

Built only from the cached quota row (and provider snapshots), never from the
network. Each section is ``{"id", "title", "items": [...]}``; each item is

    {"label", "usedPercent", "remainingPercent", "used", "limit", "remaining",
     "unit", "resetsAt", "note"}

Missing facts are ``None``. Percentages are 0–100 *used* (``usedPercent``)
with ``remainingPercent = 100 - usedPercent`` when known. ``unit`` names the
unit of ``used/limit/remaining`` (``usd``, ``credits``, ``count``).
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _pct(value: Any) -> float | None:
    result = _num(value)
    return None if result is None else max(0.0, min(100.0, result))


def _time(value: Any) -> str | None:
    """Normalize ISO strings / epoch seconds / epoch milliseconds to RFC3339 UTC."""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value) / 1000 if float(value) > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def item(label: str, *, used_percent=None, used=None, limit=None, remaining=None,
         unit: str | None = None, resets_at=None, note: str | None = None) -> dict:
    used_pct = _pct(used_percent)
    return {
        "label": label,
        "used_percent": used_pct,
        "remaining_percent": None if used_pct is None else round(100.0 - used_pct, 4),
        "used": _num(used),
        "limit": _num(limit),
        "remaining": _num(remaining),
        "unit": unit,
        "resets_at": _time(resets_at),
        "note": note,
    }


def _section(section_id: str, title: str, items: list[dict]) -> dict | None:
    items = [value for value in items if value is not None]
    return {"id": section_id, "title": title, "items": items} if items else None


def raw_usage(row: dict | None) -> dict:
    try:
        value = json.loads((row or {}).get("raw_data") or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _windows(provider: str, row: dict, backend) -> dict | None:
    items = []
    for label, used_key, reset_key in (
        ("5 小时", "five_hour_util", "five_hour_reset"),
        ("7 天", "seven_day_util", "seven_day_reset"),
        ("30 天", "thirty_day_util", "thirty_day_reset"),
        ("Sonnet 7 天", "sonnet_util", "sonnet_reset"),
        ("Opus 7 天", "opus_util", "opus_reset"),
    ):
        if _num(row.get(used_key)) is not None:
            items.append(item(label, used_percent=row.get(used_key), resets_at=row.get(reset_key)))
    if provider == "claude":
        try:
            fable_util, fable_reset = backend.fable_display_from_quota_row(row)
        except Exception:
            fable_util, fable_reset = None, None
        if _num(fable_util) is not None:
            items.append(item("Fable 7 天", used_percent=fable_util, resets_at=fable_reset))
    return _section("windows", "用量窗口", items)


def _extra_usage(row: dict) -> dict | None:
    limit = _num(row.get("extra_limit"))
    if not limit or limit <= 0:
        return None
    used = _num(row.get("extra_used")) or 0.0
    return _section("extraUsage", "额外额度", [item(
        "额外用量", used_percent=row.get("extra_util"), used=used, limit=limit,
        remaining=max(0.0, limit - used), unit="usd",
    )])


def _openai(row: dict, usage: dict) -> list[dict | None]:
    codex = usage.get("openai") if isinstance(usage.get("openai"), dict) else {}
    items = []
    for family in codex.get("additional_rate_limits") or []:
        if not isinstance(family, dict):
            continue
        name = str(family.get("limit_name") or family.get("limit_id") or "额外额度")
        for key in ("primary", "secondary"):
            window = family.get(key) if isinstance(family.get(key), dict) else {}
            if _num(window.get("used_percent")) is None:
                continue
            minutes = _num(window.get("window_minutes"))
            if minutes and minutes > 0:
                span = (f"{minutes / 1440:g}d" if minutes % 1440 == 0 else
                        f"{minutes / 60:g}h" if minutes % 60 == 0 else f"{minutes:g}m")
            else:
                span = "主窗口" if key == "primary" else "次窗口"
            items.append(item(f"{name} {span}", used_percent=window.get("used_percent"),
                              resets_at=window.get("reset_at")))
    extra = _section("additionalLimits", "额外限额", items)
    credits_items = []
    credits = codex.get("credits") if isinstance(codex.get("credits"), dict) else {}
    balance = None
    try:
        parsed = Decimal(str(credits.get("balance")).strip()) if credits.get("balance") is not None else None
        if parsed is not None and parsed.is_finite() and parsed >= 0:
            balance = float(parsed)
    except (InvalidOperation, ValueError):
        balance = None
    if credits.get("unlimited") is True:
        credits_items.append(item("积分", unit="credits", note="不限量"))
    elif balance is not None and balance > 0:
        note = "上游标记不可用" if credits.get("has_credits") is False else None
        credits_items.append(item("积分", remaining=balance, unit="credits", note=note))
    elif credits.get("has_credits") is True:
        credits_items.append(item("积分", unit="credits", note="可用 · 余额未返回"))
    spend = codex.get("spend_control") if isinstance(codex.get("spend_control"), dict) else {}
    if spend.get("reached") is True:
        credits_items.append(item("工作区消费上限", note="已达到"))
    details = codex.get("rate_limit_reset_credit_details") or usage.get("rate_limit_reset_credit_details")
    count = row.get("openai_reset_credit_count")
    if isinstance(details, dict) and _num(details.get("available_count")) is not None:
        count = details.get("available_count")
    if _num(count) is not None:
        credits_items.append(item("官方重置卡", remaining=count, unit="count"))
    return [extra, _section("credits", "积分", credits_items)]


def _claude_reset(usage: dict) -> dict | None:
    items = []
    cedar = usage.get("cedar_ember") if isinstance(usage.get("cedar_ember"), dict) else None
    if cedar is not None:
        grants = [g for g in cedar.get("grants") or [] if isinstance(g, dict)]
        note = None if cedar.get("eligible") else (cedar.get("ineligible_reason") or None)
        items.append(item("周额度重置卡", remaining=len(grants), unit="count",
                          resets_at=cedar.get("weekly_resets_at"), note=note))
    juniper = usage.get("juniper_tide") if isinstance(usage.get("juniper_tide"), dict) else None
    if juniper is not None:
        available = juniper.get("available")
        note = "可用" if available else (juniper.get("ineligible_reason") or "不可用")
        items.append(item("5 小时重置", resets_at=juniper.get("next_available_at"), note=note))
    return _section("claudeReset", "官方额度重置", items)


def _cursor(usage: dict) -> list[dict | None]:
    cursor = usage.get("cursor") if isinstance(usage.get("cursor"), dict) else {}
    if not cursor:
        return []

    def usd(cents):
        value = _num(cents)
        return None if value is None else value / 100

    items = []
    if cursor.get("limit_cents") is not None:
        items.append(item("包含额度", used_percent=cursor.get("total_utilization"),
                          used=usd(cursor.get("total_spend_cents")), limit=usd(cursor.get("limit_cents")),
                          remaining=usd(cursor.get("remaining_cents")), unit="usd",
                          resets_at=cursor.get("billing_cycle_end"),
                          note=cursor.get("subscription_status") or None))
    if _num(cursor.get("auto_percent_used")) is not None:
        items.append(item("Cursor Models / Auto", used_percent=cursor.get("auto_percent_used"),
                          resets_at=cursor.get("billing_cycle_end")))
    if _num(cursor.get("api_percent_used")) is not None:
        items.append(item("Other Models / API", used_percent=cursor.get("api_percent_used"),
                          resets_at=cursor.get("billing_cycle_end")))
    spend = cursor.get("spend_limit") if isinstance(cursor.get("spend_limit"), dict) else {}
    if spend.get("limit_cents") is not None:
        limit, remaining = usd(spend.get("limit_cents")), usd(spend.get("remaining_cents"))
        items.append(item("额外消费上限", limit=limit, remaining=remaining, unit="usd",
                          used=None if limit is None or remaining is None else max(0.0, limit - remaining)))
    return [_section("cursorPools", "Cursor 额度", items)]


_AG_WINDOWS = {"5h": "5 小时", "weekly": "7 天", "daily": "每日", "monthly": "每月"}
_AG_ORDER = {"5h": 0, "daily": 1, "weekly": 2, "monthly": 3}


def _antigravity(usage: dict) -> list[dict | None]:
    block = usage.get("antigravity") if isinstance(usage.get("antigravity"), dict) else {}
    sections = []
    for index, group in enumerate(block.get("quota_groups") or []):
        if not isinstance(group, dict):
            continue
        items = []
        buckets = [b for b in group.get("buckets") or [] if isinstance(b, dict)]
        for bucket in sorted(buckets, key=lambda b: _AG_ORDER.get(str(b.get("window") or "").strip(), 9)):
            fraction = _num(bucket.get("remaining_fraction"))
            window = str(bucket.get("window") or "").strip()
            items.append(item(
                _AG_WINDOWS.get(window, window or "窗口"),
                used_percent=None if fraction is None else 100.0 - max(0.0, min(1.0, fraction)) * 100.0,
                resets_at=bucket.get("reset_time"),
                note=None if fraction is not None else "未知",
            ))
        name = str(group.get("display_name") or "").strip() or "模型配额"
        sections.append(_section(f"antigravityGroup{index}", name, items))
    tail = []
    error = block.get("quota_error")
    if isinstance(error, dict):
        prefix = "旧数据已保留；" if block.get("quota_groups_stale") else ""
        tail.append(item("模型配额刷新", note=prefix + str(error.get("kind") or "failed")))
    if block.get("known"):
        tail.append(item("积分", remaining=block.get("credit_amount"), unit="credits",
                         note=("可用" if block.get("available") else "已耗尽")
                         + (f" · 最低 {block.get('minimum_credit_amount')}" if block.get("minimum_credit_amount") is not None else "")))
    sections.append(_section("antigravityCredits", "积分", tail))
    return sections


def _xai(usage: dict) -> list[dict | None]:
    xai = usage.get("xai") if isinstance(usage.get("xai"), dict) else {}
    if not xai or xai.get("source") == "unsupported":
        return []
    billing = xai.get("billing") if isinstance(xai.get("billing"), dict) else {}
    errors = xai.get("errors") if isinstance(xai.get("errors"), dict) else {}
    pct = billing.get("used_percent")
    if pct is None and not errors.get("credits") and (
            billing.get("period_type") or billing.get("period_start") or billing.get("period_end")):
        pct = 0.0
    label = "7 天" if billing.get("period_type") == "USAGE_PERIOD_TYPE_WEEKLY" else "官方额度"
    items = [item(label, used_percent=pct, resets_at=billing.get("period_end"),
                  note=None if _num(pct) is not None else "上游未返回额度百分比")]
    if billing.get("on_demand_cap") is not None or billing.get("on_demand_used") is not None:
        cap, used = _num(billing.get("on_demand_cap")), _num(billing.get("on_demand_used"))
        items.append(item("按需额度", used=used, limit=cap, unit="usd",
                          remaining=None if cap is None or used is None else max(0.0, cap - used)))
    if billing.get("prepaid_balance") is not None:
        items.append(item("预付余额", remaining=billing.get("prepaid_balance"), unit="usd"))
    if billing.get("is_unified_billing_user") is not None:
        items.append(item("统一计费", note="是" if billing.get("is_unified_billing_user") else "否"))
    if errors:
        items.append(item("补充接口", note="部分补充接口刷新失败，已保留主账单数据"))
    return [_section("xaiBilling", "官方账单", items)]


def _zhipu(snapshot: dict | None) -> dict | None:
    snap = snapshot if isinstance(snapshot, dict) else {}
    items = []
    for limit in snap.get("limits") or []:
        if not isinstance(limit, dict):
            continue
        if limit.get("type") == "TIME_LIMIT" and limit.get("unit") == 5 and limit.get("number") == 1:
            if _num(limit.get("remaining")) is not None:
                items.append(item("工具调用 / 月", remaining=limit.get("remaining"), unit="count"))
    mcp = snap.get("mcp") if isinstance(snap.get("mcp"), dict) else {}
    used, limit, remaining = _num(mcp.get("used")), _num(mcp.get("limit")), _num(mcp.get("remaining"))
    if limit and used is not None:
        items.append(item("MCP / 日", used_percent=used / limit * 100, used=used, limit=limit,
                          remaining=remaining, unit="count"))
    elif remaining is not None:
        items.append(item("MCP / 日", remaining=remaining, unit="count"))
    return _section("zhipuLimits", "智谱额度", items)


def _workbuddy(snapshot: dict | None) -> dict | None:
    """TG workbuddy usage_block: credits used/remaining/capacity, packages."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    credits = snap.get("credits") if isinstance(snap.get("credits"), dict) else {}
    if not credits and snap.get("last_success_credits"):
        credits = snap["last_success_credits"]
    items = []
    if credits:
        capacity, remaining, used = (_num(credits.get(k)) for k in ("capacity", "remaining", "used"))
        if used is None and capacity is not None and remaining is not None and remaining <= capacity:
            used = capacity - remaining
        label = "企业积分" if credits.get("scope") == "enterprise" else "积分"
        complete = snap.get("complete") is True
        items.append(item(label, used_percent=(used * 100 / capacity) if capacity and used is not None else None,
                          used=used, limit=capacity, remaining=remaining, unit="credits",
                          note=None if complete else "数据不完整"))
    personal = snap.get("personal_credits") if isinstance(snap.get("personal_credits"), dict) else {}
    if personal and credits.get("scope") == "enterprise":
        items.append(item("个人积分（独立）", limit=personal.get("capacity"), remaining=personal.get("remaining"),
                          used=personal.get("used"), unit="credits"))
    packages = [p for p in snap.get("packages") or [] if isinstance(p, dict)]
    if packages:
        ends = [p.get("expires_at") for p in packages if p.get("expires_at")]
        items.append(item("资源包", remaining=len(packages), unit="count",
                          resets_at=min(ends) if ends else None, note="resetsAt=最近到期"))
    return _section("workbuddyCredits", "积分", items)


def _qoder(snapshot: dict | None) -> dict | None:
    """TG qoder usage_block: plan + expiry, credit pools."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    usage = snap.get("usage") if isinstance(snap.get("usage"), dict) else {}
    items = []
    if usage.get("plan") or usage.get("until"):
        items.append(item("套餐", resets_at=usage.get("until"), note=str(usage.get("plan") or "?")))
    for pool in usage.get("pools") or []:
        if isinstance(pool, dict):
            # Upstream names (Credits / Add-on credits / ...) are all one currency; TG shows 积分 too.
            items.append(item("积分", used_percent=pool.get("used_percent"),
                              limit=pool.get("total"), remaining=pool.get("remaining"), unit="credits"))
    return _section("qoderCredits", "积分", items)


def build_usage_sections(provider: str, row: dict | None, backend, *, zhipu_snapshot=None,
                         provider_snapshot=None) -> tuple[dict, ...]:
    """Ordered usage sections for one account; empty when nothing is cached."""
    row = row if isinstance(row, dict) else {}
    usage = raw_usage(row)
    sections: list[dict | None] = []
    if provider in {"claude", "openai", "zhipu"}:
        sections.append(_windows(provider, row, backend))
    if provider == "claude":
        sections.append(_claude_reset(usage))
    if provider == "openai":
        sections.extend(_openai(row, usage))
    if provider in {"claude", "openai"}:
        sections.append(_extra_usage(row))
    if provider == "cursor":
        sections.extend(_cursor(usage))
    if provider == "antigravity":
        sections.extend(_antigravity(usage))
    if provider == "xai":
        sections.extend(_xai(usage))
    if provider == "zhipu":
        sections.append(_zhipu(zhipu_snapshot))
    if provider == "workbuddy":
        sections.append(_workbuddy(provider_snapshot))
    if provider == "qoder":
        sections.append(_qoder(provider_snapshot))
    return tuple(section for section in sections if section)
