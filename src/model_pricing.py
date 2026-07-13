"""模型 Token 费用估算。

价格目录来自 LiteLLM ``model_prices_and_context_window.json``。运行时先加载
随仓库提供的本地快照，再用远端目录异步刷新并写入 data 目录缓存；远端不可用
不会影响请求处理。

当前 ``request_log`` 的 input_tokens 已扣除 cache_read_tokens，因此四类 Token
可直接分别计价。旧日志若无法确认这一口径，由 log_db 标记为未计价，不能再从
input 中猜测扣减缓存命中。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping

from . import config

TICKS_PER_USD = 10_000_000_000
_DEFAULT_SOURCE_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
_BUNDLED_PATH = os.path.join(
    os.path.dirname(__file__), "resources", "model_prices_and_context_window.json"
)
_CACHE_FILENAME = "model_pricing.json"
_MAX_REMOTE_CATALOG_BYTES = 16 * 1024 * 1024
_DATE_SUFFIX_RE = re.compile(r"(?:-\d{8}|-\d{4}-\d{2}-\d{2})$")
_ABOVE_TOKEN_RE = re.compile(r"_above_(\d+)([kKmM])_tokens(?:_priority)?$")
_COST_TICKS_RE = re.compile(
    r'"cost_in_usd_ticks"\s*:\s*"?(\d+)"?', re.IGNORECASE
)
_PROVIDER_PREFIXES = (
    "openai/",
    "anthropic/",
    "xai/",
    "gemini/",
    "google/",
    "vertex_ai/",
    "azure/",
    "bedrock/",
    "litellm_proxy/",
)


@dataclass(frozen=True)
class PricingEntry:
    input_per_token: float
    output_per_token: float
    cache_write_per_token: float
    cache_read_per_token: float
    priority_input_per_token: float | None = None
    priority_output_per_token: float | None = None
    priority_cache_write_per_token: float | None = None
    priority_cache_read_per_token: float | None = None
    long_context_input_threshold: int = 0
    long_context_input_multiplier: float = 1.0
    long_context_output_multiplier: float = 1.0
    above_input_per_token: float | None = None
    above_output_per_token: float | None = None
    above_cache_write_per_token: float | None = None
    above_cache_read_per_token: float | None = None
    priority_above_input_per_token: float | None = None
    priority_above_output_per_token: float | None = None
    priority_above_cache_write_per_token: float | None = None
    priority_above_cache_read_per_token: float | None = None
    fast_multiplier: float = 1.0
    cache_write_1h_per_token: float | None = None


@dataclass(frozen=True)
class CostEstimate:
    total_ticks: int
    input_ticks: int
    output_ticks: int
    cache_write_ticks: int
    cache_read_ticks: int
    pricing_model: str


@dataclass(frozen=True)
class PricingSettings:
    enabled: bool
    aliases: Mapping[str, str]
    overrides: Mapping[str, PricingEntry]


_lock = threading.RLock()
_catalog: dict[str, PricingEntry] = {}
_initialized = False
_catalog_source = "none"


def _nonnegative_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result < 0 or result != result:  # NaN
        return None
    return result


def _positive_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _above_token_price(
    raw: Mapping[str, Any],
    field: str,
    *,
    priority: bool,
) -> tuple[int, float] | None:
    """Read the highest explicit ``above_Nk_tokens`` tariff for one field."""

    prefix = f"{field}_above_"
    suffix = "_priority" if priority else ""
    candidates: list[tuple[int, float]] = []
    for key, value in raw.items():
        key_text = str(key)
        if not key_text.startswith(prefix) or not key_text.endswith(suffix):
            continue
        if not priority and key_text.endswith("_priority"):
            continue
        match = _ABOVE_TOKEN_RE.search(key_text)
        price = _nonnegative_float(value)
        if match is not None and price is not None:
            scale = 1_000 if match.group(2).lower() == "k" else 1_000_000
            candidates.append((int(match.group(1)) * scale, price))
    return max(candidates, default=None, key=lambda item: item[0])


def _entry_from_litellm(raw: Any) -> PricingEntry | None:
    if not isinstance(raw, Mapping):
        return None
    input_price = _nonnegative_float(raw.get("input_cost_per_token"))
    output_price = _nonnegative_float(raw.get("output_cost_per_token"))
    # 只有图片价、按次价等条目不能被误当作 $0 的 Token 模型。
    if input_price is None or output_price is None:
        return None
    cache_write = _nonnegative_float(raw.get("cache_creation_input_token_cost"))
    cache_read = _nonnegative_float(raw.get("cache_read_input_token_cost"))
    all_explicit_thresholds: set[int] = set()
    for key in raw:
        match = _ABOVE_TOKEN_RE.search(str(key))
        if match is not None:
            scale = 1_000 if match.group(2).lower() == "k" else 1_000_000
            all_explicit_thresholds.add(int(match.group(1)) * scale)
    if len(all_explicit_thresholds) > 1:
        return None
    above_fields = {
        "input": _above_token_price(raw, "input_cost_per_token", priority=False),
        "output": _above_token_price(raw, "output_cost_per_token", priority=False),
        "cache_write": _above_token_price(
            raw, "cache_creation_input_token_cost", priority=False
        ),
        "cache_read": _above_token_price(
            raw, "cache_read_input_token_cost", priority=False
        ),
        "priority_input": _above_token_price(
            raw, "input_cost_per_token", priority=True
        ),
        "priority_output": _above_token_price(
            raw, "output_cost_per_token", priority=True
        ),
        "priority_cache_write": _above_token_price(
            raw, "cache_creation_input_token_cost", priority=True
        ),
        "priority_cache_read": _above_token_price(
            raw, "cache_read_input_token_cost", priority=True
        ),
    }
    thresholds = [item[0] for item in above_fields.values() if item is not None]
    declared_threshold = _positive_int(raw.get("long_context_input_token_threshold"))
    if declared_threshold is not None:
        thresholds.append(declared_threshold)
    # The current catalog uses one threshold per model.  If it ever grows
    # multiple tiers (or conflicts with the declared threshold), fail this
    # model closed instead of silently selecting the wrong tariff with a
    # single-tier data structure.
    if len(set(thresholds)) > 1:
        return None
    threshold = min(thresholds) if thresholds else 0

    def above(name: str) -> float | None:
        item = above_fields[name]
        return item[1] if item is not None and item[0] == threshold else None

    return PricingEntry(
        input_per_token=input_price,
        output_per_token=output_price,
        # 缺少专用缓存价时按普通输入价估算，宁可保守也不把已用 Token 算成 0。
        cache_write_per_token=input_price if cache_write is None else cache_write,
        cache_read_per_token=input_price if cache_read is None else cache_read,
        priority_input_per_token=_nonnegative_float(raw.get("input_cost_per_token_priority")),
        priority_output_per_token=_nonnegative_float(raw.get("output_cost_per_token_priority")),
        priority_cache_write_per_token=_nonnegative_float(
            raw.get("cache_creation_input_token_cost_priority")
        ),
        priority_cache_read_per_token=_nonnegative_float(
            raw.get("cache_read_input_token_cost_priority")
        ),
        long_context_input_threshold=threshold,
        long_context_input_multiplier=(
            _nonnegative_float(raw.get("long_context_input_cost_multiplier")) or 1.0
        ),
        long_context_output_multiplier=(
            _nonnegative_float(raw.get("long_context_output_cost_multiplier")) or 1.0
        ),
        above_input_per_token=above("input"),
        above_output_per_token=above("output"),
        above_cache_write_per_token=above("cache_write"),
        above_cache_read_per_token=above("cache_read"),
        priority_above_input_per_token=above("priority_input"),
        priority_above_output_per_token=above("priority_output"),
        priority_above_cache_write_per_token=above("priority_cache_write"),
        priority_above_cache_read_per_token=above("priority_cache_read"),
        fast_multiplier=(
            _nonnegative_float(
                (raw.get("provider_specific_entry") or {}).get("fast")
                if isinstance(raw.get("provider_specific_entry"), Mapping)
                else None
            )
            or 1.0
        ),
        cache_write_1h_per_token=_nonnegative_float(
            raw.get("cache_creation_input_token_cost_above_1hr")
        ),
    )


def _parse_catalog(payload: Any) -> dict[str, PricingEntry]:
    if not isinstance(payload, Mapping):
        raise ValueError("pricing catalog must be a JSON object")
    parsed: dict[str, PricingEntry] = {}
    for model, raw in payload.items():
        if not isinstance(model, str) or not model.strip():
            continue
        entry = _entry_from_litellm(raw)
        if entry is not None:
            parsed[model.strip().lower()] = entry
    if not parsed:
        raise ValueError("pricing catalog contains no token-priced models")
    return parsed


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def initialize() -> None:
    """同步加载缓存或随包快照；不发网络请求，可安全放在 lifespan 启动阶段。"""

    global _catalog, _initialized, _catalog_source
    with _lock:
        if _initialized:
            return
        candidates = (
            (os.path.join(config.DATA_DIR, _CACHE_FILENAME), "cache"),
            (_BUNDLED_PATH, "bundled"),
        )
        last_error: Exception | None = None
        for path, source in candidates:
            try:
                parsed = _parse_catalog(_load_json(path))
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                last_error = exc
                continue
            _catalog = parsed
            _catalog_source = source
            _initialized = True
            return
        raise RuntimeError(f"failed to load model pricing catalog: {last_error}")


def reset_for_tests() -> None:
    global _catalog, _initialized, _catalog_source
    with _lock:
        _catalog = {}
        _initialized = False
        _catalog_source = "none"


def _entry_from_override(raw: Any) -> PricingEntry | None:
    """解析配置覆盖；价格单位均为 USD / 1M Token。"""

    if not isinstance(raw, Mapping):
        return None
    input_m = _nonnegative_float(raw.get("inputPerMillion"))
    output_m = _nonnegative_float(raw.get("outputPerMillion"))
    if input_m is None or output_m is None:
        return None

    def per_token(name: str, fallback: float | None = None) -> float | None:
        value = _nonnegative_float(raw.get(name))
        if value is None:
            return fallback
        return value / 1_000_000

    input_p = input_m / 1_000_000
    output_p = output_m / 1_000_000
    return PricingEntry(
        input_per_token=input_p,
        output_per_token=output_p,
        cache_write_per_token=per_token("cacheWritePerMillion", input_p) or 0.0,
        cache_read_per_token=per_token("cacheReadPerMillion", input_p) or 0.0,
        priority_input_per_token=per_token("priorityInputPerMillion"),
        priority_output_per_token=per_token("priorityOutputPerMillion"),
        priority_cache_write_per_token=per_token("priorityCacheWritePerMillion"),
        priority_cache_read_per_token=per_token("priorityCacheReadPerMillion"),
    )


def settings(cfg: Mapping[str, Any] | None = None) -> PricingSettings:
    pricing_cfg = (cfg or config.get()).get("pricing", {})
    if not isinstance(pricing_cfg, Mapping):
        pricing_cfg = {}
    aliases_raw = pricing_cfg.get("aliases", {})
    aliases: dict[str, str] = {}
    if isinstance(aliases_raw, Mapping):
        for alias, target in aliases_raw.items():
            if isinstance(alias, str) and isinstance(target, str) and alias.strip() and target.strip():
                aliases[alias.strip().lower()] = target.strip().lower()
    overrides_raw = pricing_cfg.get("overrides", {})
    overrides: dict[str, PricingEntry] = {}
    if isinstance(overrides_raw, Mapping):
        for model, raw in overrides_raw.items():
            if not isinstance(model, str) or not model.strip():
                continue
            entry = _entry_from_override(raw)
            if entry is not None:
                overrides[model.strip().lower()] = entry
    return PricingSettings(
        enabled=bool(pricing_cfg.get("enabled", True)),
        aliases=aliases,
        overrides=overrides,
    )


def _model_candidates(model: str, aliases: Mapping[str, str]) -> list[str]:
    normalized = str(model or "").strip().lower()
    if not normalized:
        return []
    normalized = aliases.get(normalized, normalized)
    candidates: list[str] = [normalized]
    for prefix in _PROVIDER_PREFIXES:
        if normalized.startswith(prefix):
            candidates.append(normalized[len(prefix) :])
            break
    # 日期后缀只作为 exact miss 后的保守 fallback；不做任意 family 模糊匹配。
    for item in tuple(candidates):
        stripped = _DATE_SUFFIX_RE.sub("", item)
        if stripped and stripped != item:
            candidates.append(stripped)
    return list(dict.fromkeys(candidates))


def resolve_price(
    model: str,
    *,
    pricing_settings: PricingSettings | None = None,
) -> tuple[str, PricingEntry] | None:
    if not _initialized:
        try:
            initialize()
        except Exception:
            return None
    current = pricing_settings or settings()
    if not current.enabled:
        return None
    candidates = _model_candidates(model, current.aliases)
    for candidate in candidates:
        override = current.overrides.get(candidate)
        if override is not None:
            return candidate, override
    with _lock:
        for candidate in candidates:
            entry = _catalog.get(candidate)
            if entry is not None:
                return candidate, entry
    return None


def long_context_threshold(
    model: str,
    *,
    pricing_settings: PricingSettings | None = None,
) -> int:
    """Return the resolved request-wide long-context threshold, or ``0``."""

    resolved = resolve_price(model, pricing_settings=pricing_settings)
    return int(resolved[1].long_context_input_threshold) if resolved is not None else 0


def has_ambiguous_cache_write_ttl(
    model: str,
    *,
    pricing_settings: PricingSettings | None = None,
) -> bool:
    """Whether cache-write tokens need a TTL split that logs do not retain."""

    resolved = resolve_price(model, pricing_settings=pricing_settings)
    if resolved is None:
        return False
    entry = resolved[1]
    one_hour = entry.cache_write_1h_per_token
    return bool(
        one_hour is not None
        and one_hour > 0
        and abs(one_hour - entry.cache_write_per_token) > 1e-18
    )


def _ticks(tokens: int, price_per_token: float) -> int:
    token_count = max(0, int(tokens or 0))
    value = Decimal(token_count) * Decimal(str(price_per_token)) * Decimal(TICKS_PER_USD)
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _effective_price(
    entry: PricingEntry,
    base_name: str,
    *,
    priority: bool,
    long_context: bool,
    input_side: bool,
) -> float:
    base = float(getattr(entry, base_name))
    priority_name = f"priority_{base_name}"
    priority_price = getattr(entry, priority_name)
    used_priority_price = bool(priority and priority_price is not None)
    if used_priority_price:
        base = float(priority_price)

    if long_context:
        above_name = f"above_{base_name}"
        if priority:
            priority_above = getattr(entry, f"priority_{above_name}")
            above = priority_above if priority_above is not None else getattr(entry, above_name)
            if priority_above is not None:
                used_priority_price = True
        else:
            above = getattr(entry, above_name)
        if above is not None:
            base = float(above)
        else:
            multiplier = (
                entry.long_context_input_multiplier
                if input_side
                else entry.long_context_output_multiplier
            )
            if multiplier > 1:
                base *= multiplier

    # Anthropic Fast uses a catalog multiplier instead of *_priority fields.
    # Do not stack it on providers that already supplied an explicit priority
    # tariff for this component.
    if priority and not used_priority_price and entry.fast_multiplier > 1:
        base *= entry.fast_multiplier
    return base


def estimate_cost(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    priority: bool = False,
    long_context: bool | None = None,
    pricing_settings: PricingSettings | None = None,
) -> CostEstimate | None:
    resolved = resolve_price(model, pricing_settings=pricing_settings)
    if resolved is None:
        return None
    pricing_model, entry = resolved
    # Anthropic exposes different 5-minute and 1-hour cache-write tariffs,
    # while request_log currently retains only their combined token count.
    # Returning a precise-looking estimate would silently choose the wrong
    # tariff for one of the two cases, so fail closed for those requests.
    if (
        max(0, int(cache_creation_tokens or 0)) > 0
        and has_ambiguous_cache_write_ttl(
            pricing_model, pricing_settings=pricing_settings
        )
    ):
        return None
    prompt_tokens = (
        max(0, int(input_tokens or 0))
        + max(0, int(cache_creation_tokens or 0))
        + max(0, int(cache_read_tokens or 0))
    )
    use_long_context = (
        bool(long_context)
        if long_context is not None
        else bool(
            entry.long_context_input_threshold > 0
            and prompt_tokens > entry.long_context_input_threshold
        )
    )
    input_price = _effective_price(
        entry,
        "input_per_token",
        priority=priority,
        long_context=use_long_context,
        input_side=True,
    )
    output_price = _effective_price(
        entry,
        "output_per_token",
        priority=priority,
        long_context=use_long_context,
        input_side=False,
    )
    cache_write_price = _effective_price(
        entry,
        "cache_write_per_token",
        priority=priority,
        long_context=use_long_context,
        input_side=True,
    )
    cache_read_price = _effective_price(
        entry,
        "cache_read_per_token",
        priority=priority,
        long_context=use_long_context,
        input_side=True,
    )
    input_ticks = _ticks(input_tokens, input_price)
    output_ticks = _ticks(output_tokens, output_price)
    cache_write_ticks = _ticks(cache_creation_tokens, cache_write_price)
    cache_read_ticks = _ticks(cache_read_tokens, cache_read_price)
    return CostEstimate(
        total_ticks=input_ticks + output_ticks + cache_write_ticks + cache_read_ticks,
        input_ticks=input_ticks,
        output_ticks=output_ticks,
        cache_write_ticks=cache_write_ticks,
        cache_read_ticks=cache_read_ticks,
        pricing_model=pricing_model,
    )


def _extract_ticks_from_obj(obj: Any) -> int | None:
    if not isinstance(obj, Mapping):
        return None
    candidates = [obj]
    for key in ("response", "data"):
        nested = obj.get(key)
        if isinstance(nested, Mapping):
            candidates.append(nested)
    for candidate in candidates:
        usage = candidate.get("usage")
        if not isinstance(usage, Mapping):
            continue
        raw = usage.get("cost_in_usd_ticks")
        try:
            ticks = int(raw)
        except (TypeError, ValueError):
            continue
        if ticks >= 0:
            return ticks
    return None


def extract_actual_cost_ticks(response_body: Any) -> int | None:
    """提取 xAI 返回的真实 ``cost_in_usd_ticks``，同时兼容 JSON 与 SSE。"""

    if response_body is None:
        return None
    if isinstance(response_body, Mapping):
        return _extract_ticks_from_obj(response_body)
    if isinstance(response_body, bytes):
        text = response_body.decode("utf-8", errors="replace")
    else:
        text = str(response_body)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = None
    ticks = _extract_ticks_from_obj(parsed)
    if ticks is not None:
        return ticks
    # SSE 与截断到尾部的超大 JSON 都可以直接从最后一次 usage 中提取。
    # finditer 不会像 splitlines() 那样再复制整段响应。
    last_ticks: int | None = None
    for match in _COST_TICKS_RE.finditer(text):
        try:
            last_ticks = int(match.group(1))
        except (TypeError, ValueError):
            continue
    if last_ticks is not None:
        return last_ticks
    return None


async def refresh_once() -> bool:
    """从远端刷新目录并原子落盘；失败时保留当前目录。"""

    pricing_cfg = config.get().get("pricing", {})
    if (
        not isinstance(pricing_cfg, Mapping)
        or not pricing_cfg.get("enabled", True)
        or not pricing_cfg.get("autoUpdate", True)
    ):
        return False
    url = str(pricing_cfg.get("sourceUrl") or _DEFAULT_SOURCE_URL).strip()
    if not url.startswith("https://"):
        raise ValueError("pricing.sourceUrl must use https://")
    from . import upstream

    response = await upstream.get_client().get(url, timeout=20.0)
    response.raise_for_status()
    raw_payload = response.content
    if len(raw_payload) > _MAX_REMOTE_CATALOG_BYTES:
        raise ValueError(
            f"pricing catalog exceeds {_MAX_REMOTE_CATALOG_BYTES} bytes"
        )
    cache_path = os.path.join(config.DATA_DIR, _CACHE_FILENAME)

    def parse_and_store() -> dict[str, PricingEntry]:
        payload = json.loads(raw_payload)
        parsed_catalog = _parse_catalog(payload)
        # 防止上游异常页或被截断的小对象覆盖可用缓存。
        if len(parsed_catalog) < 100:
            raise ValueError(
                f"pricing catalog unexpectedly small: {len(parsed_catalog)}"
            )
        tmp_path = f"{cache_path}.tmp"
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        try:
            with open(tmp_path, "wb") as handle:
                handle.write(raw_payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, cache_path)
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
        return parsed_catalog

    parsed = await asyncio.to_thread(parse_and_store)
    global _catalog, _catalog_source, _initialized
    with _lock:
        _catalog = parsed
        _catalog_source = "remote"
        _initialized = True
    return True


async def refresh_loop() -> None:
    """后台更新循环。任何异常只记一行日志，不影响代理请求。"""

    while True:
        try:
            await refresh_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[Pricing] refresh failed, keeping {_catalog_source} catalog: {exc}")
        pricing_cfg = config.get().get("pricing", {})
        raw_hours = pricing_cfg.get("refreshHours", 24) if isinstance(pricing_cfg, Mapping) else 24
        try:
            hours = max(1.0, float(raw_hours))
        except (TypeError, ValueError):
            hours = 24.0
        await asyncio.sleep(hours * 3600)


def catalog_status() -> dict[str, Any]:
    with _lock:
        return {"source": _catalog_source, "models": len(_catalog)}
