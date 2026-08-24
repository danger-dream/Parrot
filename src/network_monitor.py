"""Network health monitor.

Runs lightweight connectivity checks for DNS, SOCKS5, configured API channels,
and core upstreams. Notifications are edge-triggered:
- ok/unknown -> failed: send one failure notification
- failed -> ok: send one recovery notification
Persistent status is stored in StateStore snapshots so menus can show banners and details.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlparse

import httpx

from . import config, network, notifier, state_db
from .channel import registry
from .channel.url_utils import resolve_upstream_url


def _has_family_account(family: str) -> bool:
    """检查 config 中是否存在该家族的可用账号或 API 渠道。
    family: "anthropic" | "openai" | "cloudflare"
    cloudflare 没有账号概念，永远返回 True（仅靠用户开关控制）。
    """
    if family == "cloudflare":
        return True
    try:
        cfg_data = config.get()
    except Exception:
        return False
    # 看 OAuth 账号
    for acc in cfg_data.get("oauthAccounts", []) or []:
        prov = (acc.get("provider") or "").lower()
        if family == "openai" and prov in ("openai", "xai", "cursor"):
            return True
        if family == "anthropic" and prov in ("", "anthropic", "claude"):
            return True
    # 看 API 渠道（按 protocol 推断家族）
    for entry in cfg_data.get("channels", []) or []:
        proto = (entry.get("protocol") or "anthropic").lower()
        if family == "openai" and proto.startswith("openai"):
            return True
        if family == "anthropic" and not proto.startswith("openai"):
            return True
    return False


def prune_orphan_channel_toggles() -> int:
    """清掉 monitor.channels.byKey 中已不存在的 channel key。返回清理数量。"""
    try:
        live = {ch.key for ch in registry.all_channels()}
    except Exception:
        return 0
    removed_count = 0

    def _mutator(cfg_data: dict) -> None:
        nonlocal removed_count
        mon = ((cfg_data.setdefault("network", {})).setdefault("monitor", {}))
        ch_cfg = mon.setdefault("channels", {"enabled": False, "byKey": {}})
        by_key = ch_cfg.setdefault("byKey", {})
        cleaned = {k: v for k, v in by_key.items() if k in live}
        removed_count = len(by_key) - len(cleaned)
        if removed_count:
            ch_cfg["byKey"] = cleaned

    try:
        config.update(_mutator)
    except Exception:
        return 0
    return removed_count


@dataclass
class CheckResult:
    key: str
    label: str
    category: str
    ok: bool
    detail: str = ""
    latency_ms: int | None = None


_CORE_TARGETS: dict[str, tuple[str, str]] = {
    "openai": ("OpenAI", "https://api.openai.com/"),
    "claude": ("Claude", "https://api.anthropic.com/"),
    "cloudflare": ("Cloudflare", "https://www.cloudflare.com/cdn-cgi/trace"),
}

_DNS_TARGETS = (
    ("chatgpt.com", "ChatGPT"),
    ("api.anthropic.com", "Claude/Anthropic"),
    ("api.telegram.org", "Telegram"),
    ("api.github.com", "GitHub"),
)

_SOCKS5_TEST_URL = "https://www.cloudflare.com/cdn-cgi/trace"
_SOCKS5_MAX_CONCURRENCY = 4
_SOCKS5_ROUND_DEADLINE_FACTOR = 2.0
_SOCKS5_MIN_ROUND_DEADLINE_SECONDS = 0.1
_loop_task: asyncio.Task | None = None

# Settings are changed by synchronous Telegram handlers, while the background
# loop and manual ``asyncio.run`` calls may use different event loops/threads.
# A process-wide threading lock therefore protects the generation check and the
# synchronous save/notify commit as one linearizable critical section.
_monitor_lifecycle_lock = threading.RLock()
_monitor_generation = 0


def cfg() -> dict:
    raw = (config.get().get("network") or {}).get("monitor") or {}
    interval = int(raw.get("intervalSeconds", 60) or 60)
    if interval < 5:
        interval = 5
    return {
        "enabled": bool(raw.get("enabled", True)),
        "intervalSeconds": interval,
        "dns": bool(raw.get("dns", False)),
        "socks5": bool(raw.get("socks5", False)),
        "channels": raw.get("channels") or {"enabled": False, "byKey": {}},
        "core": raw.get("core") or {"openai": False, "claude": False, "cloudflare": False},
        "timeoutSeconds": max(1.0, float(raw.get("timeoutSeconds", 5) or 5)),
    }


def _monitor_cfg_mut(c: dict) -> dict:
    net = c.setdefault("network", {})
    mon = net.setdefault("monitor", {})
    mon.setdefault("enabled", True)
    mon.setdefault("intervalSeconds", 60)
    mon.setdefault("dns", False)
    mon.setdefault("socks5", False)
    mon.setdefault("channels", {"enabled": False, "byKey": {}})
    mon.setdefault("core", {"openai": False, "claude": False, "cloudflare": False})
    mon.setdefault("timeoutSeconds", 5)
    return mon


def update_settings(mutator) -> None:
    global _monitor_generation

    def _mut(c: dict) -> None:
        mon = _monitor_cfg_mut(c)
        mutator(mon)
        try:
            mon["intervalSeconds"] = max(5, int(mon.get("intervalSeconds", 60) or 60))
        except Exception:
            mon["intervalSeconds"] = 60

    with _monitor_lifecycle_lock:
        config.update(_mut)
        # Every successful monitor settings update invalidates rounds that
        # captured an older configuration. In particular, disable followed by
        # re-enable advances twice, so an old round can never match the new era.
        _monitor_generation += 1
        # monitor_loop skips run_once() while disabled, so clear persisted rows
        # here. The configuration write already succeeded; cleanup is strictly
        # best-effort and must not turn a successful UI toggle into a failure.
        if not cfg().get("enabled", True):
            try:
                state_db.network_check_delete_stale(set())
            except Exception as exc:
                print(
                    "[network_monitor] disabled-state cleanup failed: "
                    f"{type(exc).__name__}"
                )


def set_channel_enabled(channel_key: str, enabled: bool) -> None:
    def _mut(mon: dict) -> None:
        ch = mon.setdefault("channels", {"enabled": False, "byKey": {}})
        ch.setdefault("byKey", {})[channel_key] = bool(enabled)
    update_settings(_mut)


def channel_enabled(channel_key: str, *, default: bool = False) -> bool:
    ch_cfg = cfg().get("channels") or {}
    by_key = ch_cfg.get("byKey") or {}
    return bool(by_key.get(channel_key, default))


def enabled_channel_keys() -> set[str]:
    ch_cfg = cfg().get("channels") or {}
    if not bool(ch_cfg.get("enabled", False)):
        return set()
    by_key = ch_cfg.get("byKey") or {}
    return {str(k) for k, v in by_key.items() if v}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _result_row(res: CheckResult) -> dict:
    return {
        "key": res.key,
        "label": res.label,
        "category": res.category,
        "ok": bool(res.ok),
        "detail": res.detail or "",
        "latency_ms": res.latency_ms,
        "checked_at": _now_ms(),
    }


def _format_result(res: CheckResult) -> str:
    if res.ok:
        suffix = f" · {res.latency_ms}ms" if res.latency_ms is not None else ""
        return f"✅ {res.label}{suffix}"
    detail = f"：{res.detail}" if res.detail else ""
    return f"❌ {res.label}{detail}"


def _notify_transition(res: CheckResult, prev: Optional[dict]) -> None:
    prev_ok = None if prev is None else bool(prev.get("ok"))
    if prev_ok is False and res.ok:
        notifier.notify_event(
            "network_monitor",
            f"✅ <b>网络检测恢复</b>\n{notifier.escape_html(_format_result(res))}",
        )
    elif prev_ok is not False and not res.ok:
        notifier.notify_event(
            "network_monitor",
            f"⚠️ <b>网络检测失败</b>\n{notifier.escape_html(_format_result(res))}",
        )


def _save_result(res: CheckResult) -> None:
    prev = state_db.network_check_load(res.key)
    _notify_transition(res, prev)
    state_db.network_check_save(_result_row(res))


def _dns_check(timeout: float) -> CheckResult:
    t0 = time.time()
    failures: list[str] = []
    for host, label in _DNS_TARGETS:
        try:
            network.resolve_host(host, timeout=timeout, use_cache=False)
        except Exception as exc:
            failures.append(f"{host}: {str(exc)[:120]}")
    latency = int((time.time() - t0) * 1000)
    return CheckResult(
        key="dns",
        label="DNS 解析",
        category="dns",
        ok=not failures,
        detail="; ".join(failures[:4]),
        latency_ms=latency,
    )


def _safe_socks5_error(exc: Exception) -> str:
    """Return a useful error category without echoing proxy URLs/credentials."""
    if isinstance(exc, TimeoutError):
        return "超时"
    return type(exc).__name__


async def _socks5_check(timeout: float) -> CheckResult:
    targets: list[tuple[str, str]] = []

    # Legacy global SOCKS5 setting.
    s5 = network.socks5_cfg()
    raw = str(s5.get("url") or "").strip()
    if bool(s5.get("enabled")) and raw:
        targets.append(("全局", raw))

    # Named proxies used by account/channel/model routing.
    net = config.get().get("network") or {}
    for name, proxy_cfg in (net.get("proxies") or {}).items():
        if not isinstance(proxy_cfg, dict):
            continue
        if str(proxy_cfg.get("type") or "").strip().lower() != "socks5":
            continue
        url = str(proxy_cfg.get("url") or "").strip()
        if url:
            targets.append((str(name), url))

    if not targets:
        return CheckResult(
            key="socks5",
            label="SOCKS5 代理",
            category="socks5",
            ok=False,
            detail="SOCKS5 未配置或未启用",
        )

    per_probe_timeout = max(0.01, float(timeout))
    round_deadline = max(
        _SOCKS5_MIN_ROUND_DEADLINE_SECONDS,
        per_probe_timeout * _SOCKS5_ROUND_DEADLINE_FACTOR,
    )
    semaphore = asyncio.Semaphore(_SOCKS5_MAX_CONCURRENCY)

    async def _probe(index: int, url: str) -> tuple[int, str | None]:
        async with semaphore:
            try:
                norm = network.normalize_socks5_url(url)
                # AsyncClient resolves and connects to the proxy as part of this
                # cancellable operation; a separate synchronous pre-resolution
                # would escape the round deadline and block the event loop.
                async with httpx.AsyncClient(
                    proxy=norm.url,
                    trust_env=False,
                    http2=False,
                    timeout=httpx.Timeout(
                        connect=per_probe_timeout,
                        read=per_probe_timeout,
                        write=per_probe_timeout,
                        pool=per_probe_timeout,
                    ),
                    follow_redirects=False,
                ) as client:
                    resp = await client.get(
                        _SOCKS5_TEST_URL,
                        headers={"User-Agent": "parrot-network-monitor/0.1"},
                    )
                    if resp.status_code >= 500:
                        return index, f"HTTP {resp.status_code}"
                return index, None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return index, _safe_socks5_error(exc)

    tasks: list[asyncio.Task] = []
    task_indexes: dict[asyncio.Task, int] = {}
    for index, (_name, url) in enumerate(targets):
        task = asyncio.create_task(
            _probe(index, url),
            name=f"network-monitor-socks5-{index}",
        )
        tasks.append(task)
        task_indexes[task] = index

    t0 = time.time()
    failures_by_index: dict[int, str] = {}
    try:
        done, pending = await asyncio.wait(tasks, timeout=round_deadline)
        for task in done:
            index = task_indexes[task]
            if task.cancelled():
                failures_by_index[index] = "已取消"
                continue
            try:
                _index, error = task.result()
            except Exception as exc:
                error = _safe_socks5_error(exc)
            if error:
                failures_by_index[index] = error
        for task in pending:
            failures_by_index[task_indexes[task]] = "整轮超时"
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        # Consume every result/cancellation before returning or propagating an
        # outer cancellation, so probes cannot survive their monitoring round.
        await asyncio.gather(*tasks, return_exceptions=True)

    failures = [
        f"{name}: {failures_by_index[index]}"
        for index, (name, _url) in enumerate(targets)
        if index in failures_by_index
    ]
    latency = int((time.time() - t0) * 1000)
    detail = "; ".join(failures[:4])
    return CheckResult("socks5", "SOCKS5 代理", "socks5", not failures, detail, latency)


def _tcp_connect(host: str, port: int, timeout: float) -> int:
    t0 = time.time()
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    last_exc: Exception | None = None
    for family, socktype, proto, _canon, sockaddr in infos:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(timeout)
            sock.connect(sockaddr)
            return int((time.time() - t0) * 1000)
        except Exception as exc:
            last_exc = exc
        finally:
            try:
                sock.close()
            except Exception:
                pass
    if last_exc:
        raise last_exc
    raise OSError("no address")


def _parse_host_port(url: str) -> tuple[str, int]:
    p = urlparse(url)
    if not p.hostname:
        raise ValueError("URL 缺少 host")
    if p.port:
        return p.hostname, int(p.port)
    if p.scheme == "http":
        return p.hostname, 80
    return p.hostname, 443


def _channel_probe_url(ch) -> str:
    if getattr(ch, "type", "") == "oauth":
        if getattr(ch, "provider", "") == "cursor":
            return "https://api2.cursor.sh/aiserver.v1.AiService/AvailableModels"
        if getattr(ch, "provider", "") == "antigravity":
            return "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
        if getattr(ch, "protocol", "anthropic") == "openai-responses":
            return "https://chatgpt.com/backend-api/codex/responses"
        return "https://api.anthropic.com/api/oauth/usage"
    base = str(getattr(ch, "base_url", "") or "")
    api_path = getattr(ch, "api_path", None)
    proto = getattr(ch, "protocol", "anthropic")
    default = "/v1/messages"
    if proto == "openai-chat":
        default = "/v1/chat/completions"
    elif proto == "openai-responses":
        default = "/v1/responses"
    return resolve_upstream_url(base, api_path, default)


def _channel_check(ch, timeout: float) -> CheckResult:
    label = f"渠道 {getattr(ch, 'display_name', getattr(ch, 'key', 'unknown'))}"
    key = f"channel:{ch.key}"
    try:
        url = _channel_probe_url(ch)
        host, port = _parse_host_port(url)
        latency = _tcp_connect(host, port, timeout)
        return CheckResult(key, label, "channel", True, f"{host}:{port}", latency)
    except Exception as exc:
        return CheckResult(key, label, "channel", False, str(exc)[:180])


def _core_check(name: str, timeout: float) -> CheckResult:
    label, url = _CORE_TARGETS[name]
    try:
        host, port = _parse_host_port(url)
        latency = _tcp_connect(host, port, timeout)
        return CheckResult(f"core:{name}", label, "core", True, f"{host}:{port}", latency)
    except Exception as exc:
        return CheckResult(f"core:{name}", label, "core", False, str(exc)[:180])


async def run_once(*, save: bool = True) -> list[CheckResult]:
    if save:
        # Capture settings and generation atomically relative to update_settings.
        with _monitor_lifecycle_lock:
            c = cfg()
            round_generation: int | None = _monitor_generation
    else:
        c = cfg()
        round_generation = None

    if not c.get("enabled", True):
        # 总开关关了：清掉 StateStore snapshots 里所有遗留状态，避免主菜单 banner 永久红
        if save:
            with _monitor_lifecycle_lock:
                if (
                    round_generation == _monitor_generation
                    and not cfg().get("enabled", True)
                ):
                    try:
                        state_db.network_check_delete_stale(set())
                    except Exception as exc:
                        print(
                            "[network_monitor] stale cleanup failed: "
                            f"{type(exc).__name__}"
                        )
        return []
    timeout = float(c.get("timeoutSeconds", 5) or 5)
    out: list[CheckResult] = []
    # 本轮预期会被检测的 key 集合：跑完后用它清理 StateStore snapshots 里不再被检测的残留
    # （删账户/删渠道/关开关后，对应 key 不再进 run_once，需主动清旧 ok=false 记录）
    expected_keys: set[str] = set()

    if c.get("dns"):
        expected_keys.add("dns")
        out.append(await asyncio.to_thread(_dns_check, timeout))
    if c.get("socks5"):
        expected_keys.add("socks5")
        out.append(await _socks5_check(timeout))

    ch_cfg = c.get("channels") or {}
    if bool(ch_cfg.get("enabled", False)):
        # 先清理孤儿 toggle，避免删掉渠道后 byKey 中残留检测
        prune_orphan_channel_toggles()
        keys = enabled_channel_keys()
        for ch in registry.all_channels():
            if ch.key not in keys:
                continue
            # 只检测 API 渠道；OAuth 类型不在网络检测范围内
            if getattr(ch, "type", "") != "api":
                continue
            if not getattr(ch, "enabled", True) or getattr(ch, "disabled_reason", None):
                continue
            expected_keys.add(f"channel:{ch.key}")
            out.append(await asyncio.to_thread(_channel_check, ch, timeout))

    core = c.get("core") or {}
    # core: openai → openai 家族, claude → anthropic 家族, cloudflare → 无家族（总是允许）
    _core_family = {"openai": "openai", "claude": "anthropic", "cloudflare": "cloudflare"}
    for name in ("openai", "claude", "cloudflare"):
        if not core.get(name):
            continue
        fam = _core_family[name]
        if not _has_family_account(fam):
            # 没有对应家族账号/渠道，跳过（避免给用户无意义的失败告警）
            continue
        expected_keys.add(f"core:{name}")
        out.append(await asyncio.to_thread(_core_check, name, timeout))

    if save:
        # This lock closes the check-then-commit TOCTOU window. Whichever side
        # enters first is linearized first: either the complete save/notify
        # commit happens before the settings update, or the generation changes
        # and this old round performs no externally visible commit at all.
        with _monitor_lifecycle_lock:
            if (
                round_generation != _monitor_generation
                or not cfg().get("enabled", True)
            ):
                return out
            for res in out:
                _save_result(res)
            # 清掉 StateStore snapshots 里所有不在本轮 expected_keys 中的残留
            # 覆盖场景：删 OAuth 账户、删 API 渠道、关单项检测、关总开关
            try:
                state_db.network_check_delete_stale(expected_keys)
            except Exception as exc:
                print(
                    "[network_monitor] stale cleanup failed: "
                    f"{type(exc).__name__}"
                )
    return out


async def monitor_loop() -> None:
    while True:
        try:
            c = cfg()
            interval = int(c.get("intervalSeconds", 60) or 60)
            if c.get("enabled", True):
                await run_once(save=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[network_monitor] loop failed: {exc}")
            interval = 60
        await asyncio.sleep(max(5, interval))


def start_loop() -> asyncio.Task:
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return _loop_task
    _loop_task = asyncio.create_task(monitor_loop())
    return _loop_task


def active_failures() -> list[dict]:
    return [r for r in state_db.network_check_load_all() if not bool(r.get("ok"))]


def active_summary() -> Optional[str]:
    rows = active_failures()
    if not rows:
        return None
    counts: dict[str, int] = {}
    for r in rows:
        cat = str(r.get("category") or "other")
        counts[cat] = counts.get(cat, 0) + 1
    labels = {
        "dns": "DNS",
        "socks5": "SOCKS5",
        "channel": "渠道",
        "core": "核心上游",
    }
    parts = [f"{labels.get(k, k)} × {v}" for k, v in counts.items()]
    return "🔴 <b>网络异常</b>: " + " · ".join(parts) + " — 进入「⚙ 系统设置 → 🌐 网络设置 → 🩺 网络检测」查看详情"


def format_results(results: Iterable[CheckResult]) -> str:
    lines = ["🩺 <b>网络检测结果</b>", ""]
    any_row = False
    for res in results:
        any_row = True
        lines.append(notifier.escape_html(_format_result(res)))
    if not any_row:
        lines.append("<i>未启用任何检测项。</i>")
    return "\n".join(lines)
