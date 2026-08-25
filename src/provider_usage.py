"""API 渠道上游额度/用量：固定 endpoint adapter、独立缓存与后台协调。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from . import network, state_db


@dataclass(frozen=True)
class AdapterSpec:
    adapter: str
    host: str
    product: str
    ttl: int


# 唯一支持判定入口。Endpoint 不从渠道配置/BaseURL 推导。
SPECS: dict[tuple[str, str], AdapterSpec] = {
    ("zhipu", "coding-cn"): AdapterSpec("zhipu-coding", "open.bigmodel.cn", "coding", 300),
    ("zhipu", "coding-global"): AdapterSpec("zhipu-coding", "api.z.ai", "coding", 300),
    ("kimi", "code"): AdapterSpec("kimi-code", "api.kimi.com", "code", 180),
    ("kimi", "api-cn"): AdapterSpec("kimi-balance", "api.moonshot.cn", "api", 300),
    ("kimi", "api-global"): AdapterSpec("kimi-balance", "api.moonshot.ai", "api", 300),
    ("deepseek", "standard"): AdapterSpec("deepseek", "api.deepseek.com", "api", 300),
    ("openrouter", "standard"): AdapterSpec("openrouter", "openrouter.ai", "key", 60),
    ("minimax", "api-cn"): AdapterSpec("minimax-balance", "api.minimaxi.com", "api", 300),
    ("minimax", "api-global"): AdapterSpec("minimax-balance", "api.minimax.io", "api", 300),
    ("minimax", "token-cn"): AdapterSpec("minimax-token", "api.minimaxi.com", "token-plan", 60),
    ("minimax", "token-global"): AdapterSpec("minimax-token", "api.minimax.io", "token-plan", 60),
    ("siliconflow", "api-cn"): AdapterSpec("siliconflow", "api.siliconflow.cn", "api", 300),
    ("siliconflow", "api-global"): AdapterSpec("siliconflow", "api.siliconflow.com", "api", 300),
}

_TIMEOUT = 12.0
_MANUAL_MIN = 8
_BJT = timezone(timedelta(hours=8))
_SNAPSHOT_VERSION = 2
_WORKER_COUNT = 3
_SCAN_INTERVAL = 60
_GUARD = threading.Lock()
# Serializes registry liveness checks with cache writes/deletes so a lifecycle
# cleanup cannot race a late worker result into recreating an orphaned row.
_LIFECYCLE_LOCK = threading.RLock()
_SECRET_LOCK = threading.Lock()
_SECRET_CACHE: bytes | None = None
_INFLIGHT: set[str] = set()


def _supports_keyword(callable_obj, keyword: str) -> bool | None:
    """Preflight legacy signatures without catching callable-body TypeError."""
    try:
        params = inspect.signature(callable_obj).parameters.values()
    except (TypeError, ValueError):
        return None
    return any(
        param.kind is inspect.Parameter.VAR_KEYWORD or
        (param.name == keyword and param.kind is not inspect.Parameter.POSITIONAL_ONLY)
        for param in params
    )
_RUNTIME: dict[str, dict[str, int]] = {}
_LOOP: asyncio.AbstractEventLoop | None = None
_QUEUE: asyncio.Queue | None = None
_SCHEDULER_TASK: asyncio.Task | None = None
_WORKER_TASKS: list[asyncio.Task] = []
_RUNNING = False
_GENERATION = 0


@dataclass(frozen=True)
class RefreshJob:
    account_id: str
    spec: AdapterSpec
    key: str
    channel_key: str
    generation: int


def is_enabled() -> bool:
    """与 OAuth 主动刷新/双实例测试共用 PARROT_NO_REFRESH 禁用语义。"""
    return os.environ.get("PARROT_NO_REFRESH") != "1"


def spec_for(channel: Any) -> AdapterSpec | None:
    return SPECS.get((getattr(channel, "provider_id", None), getattr(channel, "provider_preset_id", None)))


def _secret() -> bytes:
    global _SECRET_CACHE
    with _SECRET_LOCK:
        if _SECRET_CACHE is not None:
            return _SECRET_CACHE
        key = "provider_usage_hmac_secret_v1"
        encoded = state_db.schema_meta_get(key)
        if not encoded:
            encoded = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
            state_db.schema_meta_set(key, encoded)
        _SECRET_CACHE = base64.urlsafe_b64decode(encoded.encode("ascii"))
        return _SECRET_CACHE


def account_id(channel: Any, spec: AdapterSpec | None = None) -> str | None:
    spec = spec or spec_for(channel)
    api_key = str(getattr(channel, "api_key", "") or "")
    if not spec or not api_key:
        return None
    scope = f"{spec.adapter}\0{spec.product}\0{spec.host}".encode()
    digest = hmac.new(_secret(), scope + b"\0" + api_key.encode(), hashlib.sha256).hexdigest()
    return "pu1:" + digest


def _dec(v: Any) -> str | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return format(Decimal(str(v)), "f")
    except (InvalidOperation, ValueError):
        return None


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def _pick(d: dict, *names: str) -> Any:
    for n in names:
        if d.get(n) is not None:
            return d[n]
    return None


def _pick_sources(sources: list[dict], *names: str) -> Any:
    for source in sources:
        value = _pick(source, *names)
        if value is not None:
            return value
    return None


def _epoch_ms_time(value: Any) -> str | None:
    """将绝对 epoch ms 规范为带时区的北京时间；非 epoch 值原样保留。"""
    if value is None or isinstance(value, bool):
        return None
    number = _num(value)
    if number is not None and abs(number) >= 100_000_000_000:
        try:
            return datetime.fromtimestamp(number / 1000, _BJT).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    return str(value) if str(value) else None


def _balance(label: str, value: Any, currency: Any = None, *, item_id: str | None = None) -> dict | None:
    value = _dec(value)
    if value is None:
        return None
    out = {"label": label, "value": value, "kind": "balance", "group": "balance", "unit": "currency"}
    if item_id: out["id"] = item_id
    if currency:
        out["currency"] = str(currency)
    return out


def _window(label: str, *, used=None, total=None, remaining=None, percent=None,
            reset=None, reset_in_seconds=None, status=None, start=None, end=None,
            item_id: str | None = None, unit: str = "count") -> dict:
    out: dict[str, Any] = {"label": label, "kind": "window", "group": "quota", "unit": unit}
    if item_id: out["id"] = item_id
    for k, v in (("used", used), ("total", total), ("remaining", remaining)):
        x = _dec(v)
        if x is not None:
            out[k] = x
    p = _num(percent)
    if p is not None:
        out["used_percent"] = p
    for k, v in (("reset_at", reset), ("status", status), ("start_at", start), ("end_at", end)):
        if v is not None and str(v):
            out[k] = str(v)
    duration = _num(reset_in_seconds)
    if duration is not None and duration >= 0:
        out["reset_in_seconds"] = duration
    return out


def _absolute_reset(value: Any) -> str | None:
    """Normalize only values that carry defensible absolute-time semantics."""
    if value is None or isinstance(value, bool):
        return None
    number = _num(value)
    if number is not None:
        # Magnitude establishes seconds vs milliseconds; small durations must
        # never be rendered as 1970-era epochs.
        if abs(number) >= 100_000_000_000:
            seconds = number / 1000
        elif abs(number) >= 1_000_000_000:
            seconds = number
        else:
            return None
        try:
            return datetime.fromtimestamp(seconds, _BJT).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(_BJT).isoformat(timespec="seconds")


def _base(spec: AdapterSpec) -> dict:
    return {"version": _SNAPSHOT_VERSION, "source": spec.adapter, "balances": [], "windows": [], "counters": [], "notices": [], "partial": False}


def parse_payload(spec: AdapterSpec, payload: Any, *, kind: str | None = None) -> dict:
    """解析单 endpoint payload；kind 区分多端点语义，只白名单提取。"""
    root = payload if isinstance(payload, dict) else {}
    data = root.get("data") if isinstance(root.get("data"), dict) else root
    out = _base(spec)
    if spec.adapter == "kimi-balance":
        cur = _pick(data, "currency", "unit")
        for item_id, label, names in (("available", "可用余额", ("available_balance", "availableBalance", "available")),
                             ("voucher", "赠金余额", ("voucher_balance", "voucherBalance", "voucher")),
                             ("cash", "现金余额", ("cash_balance", "cashBalance", "cash"))):
            b = _balance(label, _pick(data, *names), cur, item_id=item_id)
            if b: out["balances"].append(b)
    elif spec.adapter == "deepseek":
        available = _pick(data, "is_available", "isAvailable")
        if available is not None: out["notices"].append("账户可用" if available else "账户不可用")
        infos = _pick(data, "balance_infos", "balanceInfos") or []
        for info in infos if isinstance(infos, list) else []:
            if not isinstance(info, dict): continue
            cur = info.get("currency")
            for item_id, label, names in (("total", "总余额", ("total_balance", "totalBalance")), ("granted", "赠送余额", ("granted_balance", "grantedBalance")), ("topped_up", "充值余额", ("topped_up_balance", "toppedUpBalance"))):
                b = _balance(label, _pick(info, *names), cur, item_id=item_id)
                if b: out["balances"].append(b)
    elif spec.adapter == "openrouter":
        for item_id, label, names in (("usage_daily", "今日用量", ("usage_daily", "usageDaily")), ("usage_weekly", "本周用量", ("usage_weekly", "usageWeekly")),
                             ("usage_monthly", "本月用量", ("usage_monthly", "usageMonthly")), ("usage_total", "累计用量", ("usage", "usage_all_time")),
                             ("byok_usage", "BYOK 用量", ("byok_usage", "byokUsage")), ("key_limit", "Key 额度上限", ("limit",)), ("key_remaining", "Key 剩余额度", ("limit_remaining", "limitRemaining"))):
            b = _balance(label, _pick(data, *names), "USD", item_id=item_id)
            if b: out["balances"].append(b)
        if data.get("limit_reset") is not None: out["notices"].append(f"额度重置: {data['limit_reset']}")
        if data.get("is_free_tier") is not None: out["notices"].append("Free tier" if data["is_free_tier"] else "非 Free tier")
    elif spec.adapter == "minimax-balance":
        cur = _pick(data, "currency", "currency_type")
        for item_id, label, names in (("available", "可用余额", ("available_balance", "availableBalance")), ("cash", "现金余额", ("cash_balance", "cashBalance")),
                             ("voucher", "代金券", ("voucher_balance", "voucherBalance")), ("credit", "授信额度", ("credit_balance", "creditBalance")), ("owed", "欠款", ("owed_amount", "owed_balance", "owedBalance"))):
            b = _balance(label, _pick(data, *names), cur, item_id=item_id)
            if b: out["balances"].append(b)
    elif spec.adapter == "minimax-token":
        buckets = _pick(data, "model_remains", "modelRemains") or []
        for bucket in buckets if isinstance(buckets, list) else []:
            if not isinstance(bucket, dict): continue
            name = str(_pick(bucket, "model_name", "model", "name") or "模型额度")
            # 线上 schema 是扁平 current_interval_*/current_weekly_*；同时兼容早期嵌套别名。
            nested_current = bucket.get("current_interval") or bucket.get("currentInterval") or {}
            nested_weekly = bucket.get("weekly") or bucket.get("weekly_total") or bucket.get("weeklyTotal") or {}
            for suffix, prefix, item in (("当前周期", "current_interval", nested_current), ("每周", "current_weekly", nested_weekly)):
                total = _pick(bucket, f"{prefix}_total_count")
                used = _pick(bucket, f"{prefix}_usage_count")
                status = _pick(bucket, f"{prefix}_status")
                remaining_pct = _num(_pick(bucket, f"{prefix}_remaining_percent"))
                if isinstance(item, dict):
                    total = total if total is not None else _pick(item, "total", "total_count", "totalCount")
                    used = used if used is not None else _pick(item, "usage", "used", "used_count", "usedCount")
                    status = status if status is not None else _pick(item, "status", "state")
                remaining = None
                if _num(total) is not None and _num(used) is not None and _num(total) >= _num(used):
                    remaining = _num(total) - _num(used)
                # total=0（包括 unlimited 状态）不推导为耗尽；状态只按上游原值陈述。
                pct = (100 - remaining_pct) if remaining_pct is not None and _num(total) not in (None, 0) else None
                if pct is None and _num(total) not in (None, 0) and _num(used) is not None:
                    pct = _num(used) / _num(total) * 100
                if prefix == "current_interval":
                    start, end = bucket.get("start_time"), bucket.get("end_time")
                else:
                    start, end = bucket.get("weekly_start_time"), bucket.get("weekly_end_time")
                start, end = _epoch_ms_time(start), _epoch_ms_time(end)
                # remains_time / weekly_remains_time 是 duration ms，不能冒充绝对重置时间。
                w = _window(f"{name} · {suffix}", used=used, total=total, remaining=remaining, percent=pct,
                            reset=end, status=status, start=start, end=end)
                if any(k in w for k in ("used", "total", "remaining", "used_percent", "reset_at", "status", "start_at", "end_at")):
                    out["windows"].append(w)
    elif spec.adapter == "siliconflow":
        cur = data.get("currency")
        for item_id, label, names in (("total", "总余额", ("totalBalance", "total_balance")), ("topped_up", "充值余额", ("chargeBalance", "charge_balance")), ("available", "可用余额", ("balance",))):
            b = _balance(label, _pick(data, *names), cur, item_id=item_id)
            if b: out["balances"].append(b)
        status = _pick(data, "status", "accountStatus", "account_status")
        if status is not None: out["notices"].append(f"账户状态: {status}")
    elif spec.adapter == "kimi-code":
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        limits = data.get("limits") if isinstance(data.get("limits"), list) else []
        for item in limits:
            if not isinstance(item, dict): continue
            detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
            window = item.get("window") if isinstance(item.get("window"), dict) else {}
            if not window and isinstance(detail.get("window"), dict): window = detail["window"]
            sources = [item, detail, window]
            plain_window = item.get("window") if not isinstance(item.get("window"), dict) else None
            label = str(_pick_sources(sources, "name", "label", "type") or plain_window or "额度窗口")
            out["windows"].append(_window(label,
                used=_pick_sources(sources, "used", "usage", "current"),
                total=_pick_sources(sources, "limit", "total"),
                remaining=_pick_sources(sources, "remaining", "remain"),
                percent=_pick_sources(sources, "percentage", "used_percent", "usedPercent"),
                reset=_absolute_reset(_pick_sources(
                    sources, "reset_at", "resetAt", "reset_time", "resetTime",
                    "next_reset", "nextReset")),
                # reset_in/resetIn names establish seconds; bare ttl has no
                # unit evidence and is intentionally omitted.
                reset_in_seconds=_pick_sources(sources, "reset_in", "resetIn")))
        for k in ("used", "usage", "remaining", "limit"):
            if usage.get(k) is not None: out["counters"].append({"label": k, "value": str(usage[k])})
    elif spec.adapter == "zhipu-coding":
        if kind in (None, "quota"):
            limits = data.get("limits") if isinstance(data.get("limits"), list) else []
            for item in limits:
                if not isinstance(item, dict): continue
                typ = str(_pick(item, "type", "limitType", "name") or "额度").upper()
                unit, number = _num(item.get("unit")), _num(item.get("number"))
                if "TOKEN" in typ and unit == 3 and number == 5:
                    item_id, label = "tokens_5h", "5h"
                elif "TOKEN" in typ and unit == 6 and number == 1:
                    item_id, label = "tokens_7d", "7d"
                elif "TIME" in typ and unit == 5 and number == 1:
                    item_id, label = "mcp_month", "MCP 月度"
                else:
                    # 未确认的窗口保持上游类型语义，不猜成 token/request。
                    item_id, label = None, str(_pick(item, "type", "limitType", "name") or "额度窗口")
                out["windows"].append(_window(
                    label, item_id=item_id, unit="count",
                    used=_pick(item, "currentValue", "used"),
                    # 智谱 quota/limit 的 usage 是额度总量，不是已用量。
                    total=_pick(item, "usage", "limit", "total"),
                    remaining=_pick(item, "remaining", "remain"),
                    percent=_pick(item, "percentage", "usedPercent"),
                    reset=_epoch_ms_time(_pick(item, "nextResetTime", "resetTime", "reset_at")),
                    status=item.get("status")))
        elif kind == "model":
            total = data.get("totalUsage") if isinstance(data.get("totalUsage"), dict) else {}
            calls = total.get("totalModelCallCount")
            tokens = total.get("totalTokensUsage")
            if calls is not None:
                out["counters"].append({"id": "model_calls", "kind": "aggregate", "group": "model", "unit": "calls", "label": "模型调用", "value": str(calls)})
            if tokens is not None:
                out["counters"].append({"id": "trend_tokens", "kind": "aggregate", "group": "model", "unit": "tokens", "label": "Token 用量", "value": str(tokens)})
            summaries = data.get("modelSummaryList") or total.get("modelSummaryList") or []
            model_sum = Decimal(0)
            valid_models: list[tuple[str, str]] = []
            for item in (summaries if isinstance(summaries, list) else []):
                if not isinstance(item, dict): continue
                name, value = item.get("modelName"), _dec(item.get("totalTokens"))
                if name is not None and value is not None:
                    model_sum += Decimal(value)
                    valid_models.append((str(name), value))
            for name, value in valid_models[:8]:
                out["counters"].append({"id": "model_item", "kind": "distribution", "group": "model", "unit": "tokens", "label": name, "value": value, "distribution_total": str(model_sum)})
            if tokens is not None and _dec(tokens) is not None and model_sum != Decimal(_dec(tokens)):
                out["notices"].append("model_scope_mismatch")
        elif kind == "tool":
            total = data.get("totalUsage") if isinstance(data.get("totalUsage"), dict) else {}
            fields = (("network_search", "联网搜索", "totalNetworkSearchCount"),
                      ("web_read", "网页读取", "totalWebReadMcpCount"),
                      ("zread", "Zread", "totalZreadMcpCount"))
            parts: list[tuple[str, str, str]] = []
            for item_id, label, field in fields:
                value = _dec(total.get(field))
                if value is not None and Decimal(value) != 0:
                    parts.append((item_id, label, value))
            server_total = _dec(total.get("totalSearchMcpCount"))
            mcp_total = server_total
            if mcp_total is None and parts:
                mcp_total = str(sum((Decimal(v) for _, _, v in parts), Decimal(0)))
            if mcp_total is not None:
                out["counters"].append({"id": "mcp_total", "kind": "aggregate", "group": "tool", "unit": "calls", "label": "MCP 总调用", "value": mcp_total})
            for item_id, label, value in parts[:8]:
                out["counters"].append({"id": item_id, "kind": "breakdown", "group": "tool", "unit": "calls", "label": label, "value": value})
    return out


@dataclass(frozen=True)
class UpstreamFailure:
    message: str
    retry_after: int | None = None


class ProviderUsageError(RuntimeError):
    def __init__(self, failure: UpstreamFailure):
        super().__init__(failure.message)
        self.failure = failure


def _merge(spec: AdapterSpec, parts: list[dict], failures: list[UpstreamFailure]) -> dict:
    out = _base(spec)
    for part in parts:
        for k in ("balances", "windows", "counters", "notices"):
            out[k].extend(part.get(k) or [])
    out["partial"] = bool(failures)
    if failures:
        out["notices"].append("部分上游数据暂未获取")
        retries = [failure.retry_after for failure in failures if failure.retry_after is not None]
        if retries:
            out["_retry_after_seconds"] = max(retries)
    return out


def _friendly_error(exc: BaseException) -> tuple[str, int | None]:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        retry = None
        if status == 429:
            raw_retry = exc.response.headers.get("Retry-After", "").strip()
            try:
                retry = max(0, int(float(raw_retry)))
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(raw_retry)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    retry = max(0, int(parsed.timestamp() - time.time()))
                except (AttributeError, TypeError, ValueError, OverflowError):
                    retry = None
        if status in (401, 403): return "上游拒绝当前 Key", retry
        if status == 429: return "上游请求频率受限", retry
        if status >= 500: return "上游服务暂时不可用", retry
        return f"上游返回 HTTP {status}", retry
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)): return "上游请求超时", None
    return "上游用量暂时获取失败", None


async def _get(client: httpx.AsyncClient, url: str, key: str, *, raw_auth: bool = False) -> Any:
    headers = {"Authorization": key if raw_auth else f"Bearer {key}"}
    response = await client.get(url, headers=headers)
    response.raise_for_status()
    return response.json()


async def fetch(spec: AdapterSpec, key: str, *, channel_key: str = "") -> dict:
    """执行一次固定端点只读刷新。"""
    base = f"https://{spec.host}"
    async with network.async_client(
        timeout=_TIMEOUT,
        proxy_purpose="provider_usage",
        proxy_channel=channel_key,
    ) as client:
        if spec.adapter == "zhipu-coding":
            now = datetime.now(timezone(timedelta(hours=8)))
            start = now - timedelta(hours=24)
            fmt = "%Y-%m-%d %H:%M:%S"
            query = f"startTime={start.strftime(fmt)}&endTime={now.strftime(fmt)}"
            endpoints = [("quota", base + "/api/monitor/usage/quota/limit"),
                         ("model", base + "/api/monitor/usage/model-usage?" + query),
                         ("tool", base + "/api/monitor/usage/tool-usage?" + query)]
            parts: list[dict] = []
            failures: list[UpstreamFailure] = []
            for kind, url in endpoints:  # 每 endpoint 最多一次，不 retry
                try:
                    parts.append(parse_payload(spec, await _get(client, url, key, raw_auth=True), kind=kind))
                except Exception as exc:
                    message, retry = _friendly_error(exc)
                    failures.append(UpstreamFailure(message, retry))
            if not parts:
                retries = [failure.retry_after for failure in failures if failure.retry_after is not None]
                failure = failures[0] if failures else UpstreamFailure("上游用量暂时获取失败")
                raise ProviderUsageError(UpstreamFailure(failure.message, max(retries) if retries else None))
            return _merge(spec, parts, failures)
        paths = {"kimi-code": "/coding/v1/usages", "kimi-balance": "/v1/users/me/balance",
                 "deepseek": "/user/balance", "openrouter": "/api/v1/key",
                 "minimax-balance": "/account/query_balance", "minimax-token": "/v1/token_plan/remains",
                 "siliconflow": "/v1/user/info"}
        return parse_payload(spec, await _get(client, base + paths[spec.adapter], key))


def cached(channel: Any) -> dict:
    spec = spec_for(channel)
    if not spec: return {"status": "unsupported", "unsupported": True, "partial": False, "stale": False}
    aid = account_id(channel, spec)
    if not aid: return {"status": "unsupported", "unsupported": True, "partial": False, "stale": False}
    row = state_db.provider_usage_load(aid)
    now = int(time.time())
    with _GUARD: refreshing = aid in _INFLIGHT
    if not row:
        return {"status": "refreshing" if refreshing else "not_fetched"}
    snap = json.loads(row["snapshot_json"]) if row.get("snapshot_json") else None
    age = now - int((row.get("fetched_at") or 0) / 1000)
    error = row.get("last_error")
    if snap:
        status = "refreshing" if refreshing else "stale_error" if error else "stale" if age >= spec.ttl else ("partial" if snap.get("partial") else "fresh")
        return {"status": status, "snapshot": snap, "source": snap.get("source"),
                "fetched_at": row.get("fetched_at"), "stale": status in ("stale", "stale_error"),
                "partial": bool(snap.get("partial")), "unsupported": False, "error": error}
    return {"status": "refreshing" if refreshing else "error", "error": error, "error_at": row.get("error_at")}


def _still_live(aid: str) -> bool:
    from .channel import registry
    return any(account_id(ch) == aid for ch in registry.all_channels() if getattr(ch, "type", None) == "api" and spec_for(ch))


def cleanup_account_if_orphaned(aid: str | None) -> bool:
    """Delete cache/runtime only after the final live channel stops sharing aid."""
    if not aid:
        return False
    with _LIFECYCLE_LOCK:
        if _still_live(aid):
            return False
        state_db.provider_usage_delete(aid)
        with _GUARD:
            _RUNTIME.pop(aid, None)
        return True


def _initial_runtime(aid: str, spec: AdapterSpec) -> dict[str, int]:
    """首次见到账户时从持久缓存初始化；后续 tick 只比较内存 deadline。"""
    row = state_db.provider_usage_load(aid)
    fetched = int((row or {}).get("fetched_at") or 0)
    error = int((row or {}).get("error_at") or 0)
    retry = int((row or {}).get("retry_after") or 0)
    last_attempt = max(fetched, error)
    return {
        "last_attempt": last_attempt,
        "next_refresh_at": max(retry, last_attempt + spec.ttl * 1000 if last_attempt else 0),
        "retry_after": retry,
    }


def _enqueue_reserved(job: RefreshJob) -> None:
    """只在主 loop 执行；拒绝 stop/restart 竞态遗留的预约。"""
    with _GUARD:
        queue = _QUEUE
        valid = _RUNNING and job.generation == _GENERATION and queue is not None
        if not valid:
            _INFLIGHT.discard(job.account_id)
            return
    queue.put_nowait(job)


def schedule_refresh(channel: Any, *, force: bool = False) -> bool:
    """线程安全地预约刷新；不创建线程、task，也不等待网络。"""
    spec = spec_for(channel)
    aid = account_id(channel, spec) if spec else None
    key = str(getattr(channel, "api_key", "") or "")
    channel_key = str(getattr(channel, "key", "") or "")
    if not spec or not aid or not key or not channel_key:
        return False
    now_ms = int(time.time() * 1000)
    with _GUARD:
        loop = _LOOP
        if not _RUNNING or loop is None or loop.is_closed() or _QUEUE is None:
            return False
        runtime = _RUNTIME.get(aid)
        if runtime is None:
            # SQLite 只在首次见到账户时读取一次；RLock 并非必需，因为该调用
            # 不会回入 coordinator，普通 Lock 同时保证多线程只初始化一次。
            runtime = _initial_runtime(aid, spec)
            _RUNTIME[aid] = runtime
        deadline = (
            max(runtime["last_attempt"] + _MANUAL_MIN * 1000, runtime["retry_after"])
            if force else runtime["next_refresh_at"]
        )
        if now_ms < deadline or aid in _INFLIGHT:
            return False
        _INFLIGHT.add(aid)
        runtime["last_attempt"] = now_ms
        generation = _GENERATION
    try:
        loop.call_soon_threadsafe(_enqueue_reserved, RefreshJob(aid, spec, key, channel_key, generation))
    except RuntimeError:
        with _GUARD:
            _INFLIGHT.discard(aid)
        return False
    return True


async def _worker(worker_id: int) -> None:
    queue = _QUEUE
    assert queue is not None
    while True:
        job: RefreshJob = await queue.get()
        try:
            try:
                if _supports_keyword(fetch, "channel_key") is False:
                    snap = await fetch(job.spec, job.key)
                else:
                    snap = await fetch(
                        job.spec, job.key, channel_key=job.channel_key,
                    )
                now_ms = int(time.time() * 1000)
                retry_seconds = int(snap.pop("_retry_after_seconds", 0) or 0)
                retry_at = now_ms + retry_seconds * 1000 if retry_seconds else 0
                with _LIFECYCLE_LOCK:
                    if _still_live(job.account_id):
                        state_db.provider_usage_save_success(
                            job.account_id, job.spec.adapter, snap,
                            retry_after=retry_at or None,
                        )
                        with _GUARD:
                            runtime = _RUNTIME.get(job.account_id)
                            if runtime is not None:
                                runtime["last_attempt"] = now_ms
                                runtime["next_refresh_at"] = max(
                                    now_ms + job.spec.ttl * 1000, retry_at,
                                )
                                runtime["retry_after"] = retry_at
                    else:
                        with _GUARD:
                            _RUNTIME.pop(job.account_id, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                now_ms = int(time.time() * 1000)
                msg, retry = _friendly_error(exc)
                if isinstance(exc, ProviderUsageError):
                    msg, retry = exc.failure.message, exc.failure.retry_after
                elif isinstance(exc, RuntimeError) and str(exc) in {"上游拒绝当前 Key", "上游请求频率受限", "上游服务暂时不可用", "上游请求超时"}:
                    msg = str(exc)
                retry_at = now_ms + (retry if retry is not None else 60) * 1000
                with _LIFECYCLE_LOCK:
                    if _still_live(job.account_id):
                        state_db.provider_usage_save_error(job.account_id, job.spec.adapter, msg, retry_at)
                        with _GUARD:
                            runtime = _RUNTIME.get(job.account_id)
                            if runtime is not None:
                                runtime["last_attempt"] = now_ms
                                runtime["next_refresh_at"] = retry_at
                                runtime["retry_after"] = retry_at
                    else:
                        with _GUARD:
                            _RUNTIME.pop(job.account_id, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            # registry/SQLite 等协调层单 job 异常也不能杀死固定 worker。
            now_ms = int(time.time() * 1000)
            with _GUARD:
                runtime = _RUNTIME.get(job.account_id)
                if runtime is not None:
                    runtime["last_attempt"] = now_ms
                    runtime["next_refresh_at"] = now_ms + 60_000
                    runtime["retry_after"] = now_ms + 60_000
        finally:
            with _GUARD:
                _INFLIGHT.discard(job.account_id)
            queue.task_done()


def _live_channels() -> list[Any]:
    from .channel import registry
    return [ch for ch in registry.all_channels() if getattr(ch, "type", None) == "api"]


def _scan(channels: list[Any] | None = None, *, force: bool = False) -> dict[str, int]:
    channels = _live_channels() if channels is None else channels
    seen: set[str] = set()
    supported_channels = 0
    scheduled_accounts = 0
    for channel in channels:
        spec = spec_for(channel)
        aid = account_id(channel, spec) if spec else None
        if not spec or not aid:
            continue
        supported_channels += 1
        if aid in seen:
            continue
        seen.add(aid)
        if schedule_refresh(channel, force=force):
            scheduled_accounts += 1
    return {"supported_channels": supported_channels, "supported_accounts": len(seen),
            "scheduled_accounts": scheduled_accounts}


async def _scheduler() -> None:
    while True:
        await asyncio.sleep(_SCAN_INTERVAL)
        try:
            _scan(force=False)
        except Exception:
            # 一次热加载/registry 扫描异常不得永久终止唯一 scheduler。
            pass


async def start() -> None:
    """在 FastAPI 主 loop 创建唯一 queue、scheduler 和固定三个 worker。"""
    global _LOOP, _QUEUE, _SCHEDULER_TASK, _WORKER_TASKS, _RUNNING, _GENERATION
    loop = asyncio.get_running_loop()
    with _GUARD:
        if _RUNNING:
            if _LOOP is not loop:
                raise RuntimeError("provider usage runtime already runs on another event loop")
            return
        _GENERATION += 1
        _LOOP = loop
        _QUEUE = asyncio.Queue()
        _RUNNING = True
        _SCHEDULER_TASK = loop.create_task(_scheduler(), name="provider-usage-scheduler")
        _WORKER_TASKS = [
            loop.create_task(_worker(i), name=f"provider-usage-worker-{i + 1}")
            for i in range(_WORKER_COUNT)
        ]


async def stop() -> None:
    """停止统一 runtime；必须在 state_db 关闭前调用。"""
    global _LOOP, _QUEUE, _SCHEDULER_TASK, _WORKER_TASKS, _RUNNING, _GENERATION
    with _GUARD:
        if not _RUNNING:
            _INFLIGHT.clear()
            _RUNTIME.clear()
            return
        tasks = ([_SCHEDULER_TASK] if _SCHEDULER_TASK is not None else []) + list(_WORKER_TASKS)
        _RUNNING = False
        _GENERATION += 1
        _LOOP = None
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    with _GUARD:
        _QUEUE = None
        _SCHEDULER_TASK = None
        _WORKER_TASKS = []
        _INFLIGHT.clear()
        _RUNTIME.clear()


def schedule_startup_refresh(channels: list[Any] | None = None) -> dict[str, int]:
    """启动预热复用统一 scan/queue；只排队，不等待网络。"""
    return _scan(channels, force=True)
