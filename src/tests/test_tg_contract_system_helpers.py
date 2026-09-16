"""Deterministic executable support for the v0.31.13 system-menu traces.

This module owns no production behavior.  Every external boundary used by the
system menu is replaced with an in-memory fake before a scenario is invoked.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src.telegram import states, ui
from src.telegram.menus import system_menu as sm
from src.tests.tg_contract import TraceCapture, assert_strict_equal, load_jsonl

from src.tests.tg_contract.current import load_current_jsonl

SEGMENT = Path(__file__).parent / "fixtures/tg_contract/v0.31.13/segments/system.jsonl"
SYSTEM_IDS = frozenset(f"TG-SYS-{n:02d}" for n in range(1, 9))


def cases_for(*ids: str) -> list[dict[str, Any]]:
    wanted = set(ids)
    return [case for case in load_current_jsonl(SEGMENT) if case["capabilityId"] in wanted]


def actual_case(case: dict[str, Any], env: "SystemEnv", exception=None) -> dict[str, Any]:
    return {
        "caseId": case["caseId"],
        "capabilityId": case["capabilityId"],
        "entry": deepcopy(case["entry"]),
        "initialConfig": deepcopy(case["initialConfig"]),
        "initialState": deepcopy(case["initialState"]),
        "initialRuntime": deepcopy(case["initialRuntime"]),
        "tgApi": deepcopy(env.capture.calls),
        "stateSteps": deepcopy(env.steps),
        "finalBusinessState": {
            "config": deepcopy(env.cfg),
            "chatState": env.state_value(),
            "monitorConfig": deepcopy(env.runtime.get("monitorConfig") or {}),
            "runtimeEvents": deepcopy(env.events),
            "pendingRetention": env.pending_retention(),
        },
        "expectedException": deepcopy(exception),
    }


@dataclass
class FakeChannel:
    key: str
    display_name: str
    type: str = "api"
    family: str = "openai"
    probe_url: str = "https://probe.invalid/v1/models"


class SystemEnv:
    """Install deterministic config/clock/random/transport/business fakes."""

    def __init__(self, case: dict[str, Any], monkeypatch):
        self.case = case
        self.entry = case["entry"]
        self.fake = deepcopy(self.entry.get("fakes") or {})
        self.cfg = deepcopy(case["initialConfig"])
        self.runtime = deepcopy(case["initialRuntime"])
        self.now = float(self.runtime.get("clock", 1_770_604_800.0))
        self.events: list[dict[str, Any]] = []
        self.steps: list[dict[str, Any]] = []
        self.capture = TraceCapture()
        self._message_id = int(self.runtime.get("nextMessageId", 501))
        self._random_counter = 0
        self._install(monkeypatch)

    def _install(self, mp) -> None:
        states.clear_all()
        sm._retention_pending.clear()
        ui._code_to_name.clear()
        ui.configure("fake-system-token", [42])

        def api(method, data=None):
            self.capture.record(method, {} if data is None else data)
            if method == "sendMessage":
                result = {"ok": True, "result": {"message_id": self._message_id}}
                self._message_id += 1
                return result
            return {"ok": True, "result": {}}

        mp.setattr(ui, "api", api)
        mp.setattr(states.time, "time", lambda: self.now)
        mp.setattr(sm.time, "time", lambda: self.now)

        def token_hex(_size):
            self._random_counter += 1
            return f"{self._random_counter:032x}"

        mp.setattr(sm.secrets, "token_hex", token_hex)
        mp.setattr(sm.config, "get", lambda: self.cfg)
        mp.setattr(sm.config, "update", lambda fn: fn(self.cfg))
        mp.setattr(sm.load_balancing, "display_mode", lambda mode: {
            "smart": "智能评分", "order": "按配置顺序", "priority": "自定义优先级",
        }.get(mode, str(mode)))

        self._install_retention(mp)
        self._install_network(mp)
        self._install_monitor(mp)
        self._install_runtime(mp)

        initial = case_state = self.case.get("initialState") or {}
        if case_state:
            states.set_state(42, case_state["action"], deepcopy(case_state.get("data") or {}))
            if "ts" in case_state:
                states._states[42]["ts"] = float(case_state["ts"])

    def _install_retention(self, mp) -> None:
        def policy(cfg=None):
            source = self.cfg if cfg is None else cfg
            value = source.get("logRetention") or {"mode": "forever", "days": None}
            mode = value.get("mode")
            days = value.get("days")
            return {"mode": "days", "days": int(days)} if mode == "days" and days else {"mode": "forever", "days": None}

        def extend(days):
            self.events.append({"event": "retention.extend", "days": days})
            result = deepcopy(self.fake.get("retentionExtend", {"ok": True}))
            if result.get("ok"):
                self.cfg["logRetention"] = {"mode": "days", "days": days}
            return result

        def forever():
            self.events.append({"event": "retention.forever"})
            result = deepcopy(self.fake.get("retentionForever", {"ok": True}))
            if result.get("ok"):
                self.cfg["logRetention"] = {"mode": "forever", "days": None}
            return result

        def plan(days):
            self.events.append({"event": "retention.plan", "days": days})
            if self.fake.get("retentionPlanRaises"):
                raise RuntimeError(self.fake["retentionPlanRaises"])
            result = deepcopy(self.fake.get("retentionPlan") or {
                "days": days, "cutoff": 1_770_000_000.0, "reference_ts": self.now,
                "base_policy": policy(), "items": [], "errors": [],
                "scanned_months": 0, "scanned_bytes": 0,
                "preflight": {"ok": True, "effective_available_bytes": 4096, "required_bytes": 1024},
                "signature": "fake-plan-signature",
            })
            result["days"] = days
            return result

        def apply(got_plan, *, activate_policy=False, progress=None):
            self.events.append({"event": "retention.apply", "plan": deepcopy(got_plan), "activatePolicy": activate_policy})
            for event in self.fake.get("retentionProgress", []):
                if progress:
                    progress(deepcopy(event))
            result = deepcopy(self.fake.get("retentionApply") or {
                "ok": True, "days": got_plan.get("days"), "full_months_deleted": 0,
                "deleted_requests": 0, "actual_free_bytes": 0,
            })
            if result.get("ok") or result.get("config_saved"):
                self.cfg["logRetention"] = {"mode": "days", "days": int(result.get("days") or got_plan.get("days"))}
            return result

        mp.setattr(sm.log_db, "retention_policy", policy)
        mp.setattr(sm.log_db, "retention_cleanup_busy", lambda: bool(self.fake.get("retentionBusy")))
        mp.setattr(sm.log_db, "extend_retention_days", extend)
        mp.setattr(sm.log_db, "set_retention_forever", forever)
        mp.setattr(sm.log_db, "plan_retention", plan)
        mp.setattr(sm.log_db, "apply_retention_plan", apply)
        mp.setattr(sm.log_db, "proxy_stats", lambda limit=1000: deepcopy(self.fake.get("proxyStats") or []))

    def _install_network(self, mp) -> None:
        net = sm.network

        def parse_dns(text):
            if text == "bad-dns":
                raise ValueError("DNS 格式无效 <x>")
            return [part.strip() for part in text.replace("，", ",").split(",") if part.strip()]

        def test_dns(servers):
            self.events.append({"event": "dns.test", "servers": list(servers)})
            if self.fake.get("dnsTestRaises"):
                raise RuntimeError(self.fake["dnsTestRaises"])
            return deepcopy(self.fake.get("dnsTest") or {"ok": True, "summary": "DNS test ok"})

        def save_dns(servers):
            self.events.append({"event": "dns.save", "servers": list(servers)})
            if self.fake.get("dnsSaveRaises"):
                raise RuntimeError(self.fake["dnsSaveRaises"])
            self.cfg.setdefault("network", {}).setdefault("dns", {})["servers"] = list(servers)

        def normalize_socks(text):
            if text == "bad-socks":
                raise ValueError("SOCKS5 地址无效 <x>")
            raw = text.strip()
            if "://" not in raw:
                raw = "socks5://" + raw
            return SimpleNamespace(url=raw)

        async def test_socks(url):
            self.events.append({"event": "socks5.test", "url": url})
            if self.fake.get("socksTestRaises"):
                raise RuntimeError(self.fake["socksTestRaises"])
            return deepcopy(self.fake.get("socksTest") or {"ok": True, "summary": "SOCKS test ok"})

        def save_socks(url, enabled=True):
            self.events.append({"event": "socks5.save", "url": url, "enabled": enabled})
            if self.fake.get("socksSaveRaises"):
                raise RuntimeError(self.fake["socksSaveRaises"])
            self.cfg.setdefault("network", {})["socks5"] = {"url": url, "enabled": enabled}
            return url

        def clear_cache():
            self.events.append({"event": "dns.cache.clear"})
            if self.fake.get("cacheClearRaises"):
                raise RuntimeError(self.fake["cacheClearRaises"])

        def sync_dns():
            self.events.append({"event": "dns.sync"})
            if self.fake.get("dnsSyncRaises"):
                raise RuntimeError(self.fake["dnsSyncRaises"])
            servers = deepcopy(self.fake.get("syncedDns") or ["9.9.9.9", "149.112.112.112"])
            self.cfg.setdefault("network", {}).setdefault("dns", {})["servers"] = servers
            return servers

        def cache_entries():
            if self.fake.get("cacheReadRaises"):
                raise RuntimeError(self.fake["cacheReadRaises"])
            return deepcopy(self.fake.get("dnsCache") or [])

        mp.setattr(net, "parse_dns_input", parse_dns)
        mp.setattr(net, "test_dns_servers", test_dns)
        mp.setattr(net, "dns_test_text", lambda result: str(result.get("summary") or ("OK" if result.get("ok") else "FAIL")))
        mp.setattr(net, "failure_warning", lambda kind, result: result.get("warning"))
        mp.setattr(net, "dumps_state", deepcopy)
        mp.setattr(net, "save_dns_servers", save_dns)
        mp.setattr(net, "sync_system_dns_now", sync_dns)
        mp.setattr(net, "dns_cache_entries", cache_entries)
        mp.setattr(net, "clear_dns_cache", clear_cache)
        mp.setattr(net, "dns_servers", lambda: list((self.cfg.get("network", {}).get("dns", {}) or {}).get("servers") or ["8.8.8.8"]))
        mp.setattr(net, "normalize_socks5_url", normalize_socks)
        mp.setattr(net, "test_socks5", test_socks)
        mp.setattr(net, "socks5_test_text", lambda result: str(result.get("summary") or ("OK" if result.get("ok") else "FAIL")))
        mp.setattr(net, "save_socks5", save_socks)
        mp.setattr(net, "socks5_cfg", lambda: deepcopy(self.cfg.get("network", {}).get("socks5") or {}))
        mp.setattr(net, "mask_url", lambda url: "socks5://user:***@proxy.invalid:1080" if "@" in str(url) else str(url))

        def enable_socks(enabled):
            self.events.append({"event": "socks5.enable", "enabled": enabled})
            self.cfg.setdefault("network", {}).setdefault("socks5", {})["enabled"] = enabled
        mp.setattr(net, "set_socks5_enabled", enable_socks)

    def _install_monitor(self, mp) -> None:
        mon = deepcopy(self.runtime.get("monitorConfig") or {
            "enabled": True, "intervalSeconds": 60, "dns": False, "socks5": False,
            "core": {"openai": True, "claude": True, "cloudflare": False},
            "channels": {"enabled": False, "byKey": {}},
        })
        self.runtime["monitorConfig"] = mon
        nm = sm.network_monitor
        mp.setattr(nm, "cfg", lambda: mon)
        mp.setattr(nm, "update_settings", lambda fn: (fn(mon), self.events.append({"event": "monitor.update", "value": deepcopy(mon)}))[0])
        mp.setattr(nm, "active_failures", lambda: deepcopy(self.fake.get("activeFailures") or []))
        mp.setattr(nm, "enabled_channel_keys", lambda: [key for key, on in (mon.get("channels", {}).get("byKey", {}) or {}).items() if on])
        mp.setattr(nm, "channel_enabled", lambda key: bool((mon.get("channels", {}).get("byKey", {}) or {}).get(key)))

        def set_channel(key, enabled):
            mon.setdefault("channels", {}).setdefault("byKey", {})[key] = enabled
            self.events.append({"event": "monitor.channel", "key": key, "enabled": enabled})
        mp.setattr(nm, "set_channel_enabled", set_channel)
        mp.setattr(nm, "_channel_probe_url", lambda ch: ch.probe_url)

        async def run_once(save=True):
            self.events.append({"event": "monitor.run", "save": save})
            if self.fake.get("monitorRunRaises"):
                raise RuntimeError(self.fake["monitorRunRaises"])
            return deepcopy(self.fake.get("monitorResults") or [{"key": "dns", "ok": True}])
        mp.setattr(nm, "run_once", run_once)
        mp.setattr(nm, "format_results", lambda result: self.fake.get("monitorFormatted") or "✅ DNS · 12ms")
        mp.setattr(sm.state_db, "network_check_load_all", lambda: deepcopy(self.fake.get("monitorHistory") or []))

        channels = [FakeChannel(**item) for item in self.fake.get("channels", [])]
        mp.setattr(sm.registry, "all_channels", lambda: channels)
        mp.setattr(sm.load_balancing, "family_for_channel", lambda ch: ch.family)

    def _install_runtime(self, mp) -> None:
        mp.setattr(sm.concurrency, "totals", lambda: deepcopy(self.fake.get("concurrencyTotals") or {
            "in_flight": 0, "waiting": 0, "tracked_channels": 0,
        }))
        mp.setattr(sm.concurrency, "snapshot", lambda: deepcopy(self.fake.get("concurrencySnapshot") or []))
        mp.setattr(sm.apikey_limiter, "totals", lambda: deepcopy(self.fake.get("limiterTotals") or {
            "in_flight": 0, "waiting": 0, "tracked_keys": 0,
        }))
        mp.setattr(sm.apikey_limiter, "snapshot", lambda: deepcopy(self.fake.get("limiterSnapshot") or []))

    def state_value(self, chat_id=42):
        value = states.get_state(chat_id)
        return deepcopy(value)

    def step(self, label: str, fn, *, chat_id=42):
        before_calls = len(self.capture.calls)
        result = fn()
        self.steps.append({
            "after": label,
            "return": result,
            "state": self.state_value(chat_id),
            "newCalls": len(self.capture.calls) - before_calls,
        })
        return result

    def callback(self, data: str, label: str | None = None, *, chat_id=42, message_id=100, cb_id="cb-system"):
        result = self.step(label or data, lambda: sm.handle_callback(chat_id, message_id, cb_id, data), chat_id=chat_id)
        self.steps[-1]["callbackData"] = data
        return result

    def text(self, action: str, text: str, label: str | None = None, *, chat_id=42):
        result = self.step(label or f"{action}:{text}", lambda: sm.handle_text_state(chat_id, action, text), chat_id=chat_id)
        self.steps[-1]["stateAction"] = action
        return result

    def direct(self, label: str, fn, *, chat_id=42):
        return self.step(label, fn, chat_id=chat_id)

    def advance(self, seconds: float):
        self.now += seconds
        self.steps.append({"after": f"clock+{seconds:g}", "return": None, "state": self.state_value(), "newCalls": 0})

    def pending_retention(self):
        return [{
            "code": code, "chatId": item.get("chat_id"), "kind": item.get("kind"),
            "expiresAt": item.get("expires_at"), "days": item.get("days"),
            "plan": deepcopy(item.get("plan")),
        } for code, item in sorted(sm._retention_pending.items())]


def run_and_compare(case, monkeypatch, runner):
    env = SystemEnv(case, monkeypatch)
    exception = None
    try:
        runner(env)
    except Exception as exc:  # explicit fixture field, never silently swallowed
        exception = {"type": type(exc).__name__, "message": str(exc)}
    actual = actual_case(case, env, exception)
    assert_strict_equal(case, actual)
    return actual
