"""Shared execution support for the channel/API-key v0.31.13 traces.

This is deliberately a test-only adapter around the frozen production menus.  It
fixes time, random values, provider/model discovery, probes, Telegram transport,
and every mutable domain store.  It does not normalize Telegram payloads.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src import (
    affinity,
    apikey_limiter,
    channel_state,
    concurrency,
    config,
    cooldown,
    log_db,
    scorer,
    state_db,
)
from src.channel import registry
from src.openai.channel.api_channel import OpenAIApiChannel
from src.telegram import menu_cache, states, ui
from src.telegram.menus import apikey_menu, channel_menu, channel_wizard
from src.tests.tg_contract import load_jsonl


# Keep the v0.31.13 recording immutable; current approved model-center deltas
# are sparse overlays so every unaffected contract field remains archived data.
SEGMENT = Path(__file__).parent / "fixtures/tg_contract/v0.31.13/segments/channels_apikey.jsonl"
CURRENT_OVERRIDES = (
    Path(__file__).parent
    / "fixtures/tg_contract/model-center-2026-09-13/overrides/channels_apikey.jsonl"
)


def current_cases() -> list[dict[str, Any]]:
    archived = load_jsonl(SEGMENT)
    overrides = load_jsonl(CURRENT_OVERRIDES) if CURRENT_OVERRIDES.exists() else []
    archived_ids = {case["caseId"] for case in archived}
    override_map = {case["caseId"]: case for case in overrides}
    if len(override_map) != len(overrides):
        raise AssertionError("duplicate channel contract override caseId")
    unknown = set(override_map) - archived_ids
    if unknown:
        raise AssertionError(f"unknown channel contract overrides: {sorted(unknown)}")
    return [override_map.get(case["caseId"], case) for case in archived]


FAKE_NOW = 1_700_000_000.0
MONTH_START = 1_698_796_800.0
CONFIG_KEYS = (
    "channels",
    "apiKeys",
    "modelMapping",
    "loadBalancing",
    "apiKeyLimiter",
    "xaiOAuth",
    "errorWindows",
    "errorGraceCount",
)


def json_value(value: Any) -> Any:
    """Return the lossless JSON representation used by the JSONL manifest."""
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, set):
        return [json_value(item) for item in sorted(value)]
    return value


class MenuTransport:
    """Deterministic fake Telegram transport preserving raw method payloads."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self.next_message_id = 9001

    def __call__(self, method: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        # deepcopy only protects the captured bytes/fields from later mutation.
        self.calls.append({"method": method, "payload": deepcopy(data or {})})
        if method == "sendMessage":
            result = {"message_id": self.next_message_id}
            self.next_message_id += 1
        else:
            result = {}
        return {"ok": True, "result": result}


def _empty_period() -> dict[str, Any]:
    return {"by_channel": {}, "by_apikey": {}}


def _clear_domain_state() -> None:
    state_db.init()
    log_db.init()
    state_db.perf_delete()
    state_db.error_delete()
    state_db.affinity_delete()
    state_db.client_affinity_delete()
    states.clear_all()
    for cache in (
        menu_cache.PERIOD_STATS,
        menu_cache.DETAIL_STATS,
        menu_cache.HISTORY_TOTALS,
    ):
        cache.clear()
    for module in (cooldown, scorer, affinity):
        module._initialized = False
    cooldown.init()
    scorer.init()
    affinity.init()
    affinity.client_init()
    with channel_state.mutation_lock, concurrency._slots_guard:
        concurrency._slots.clear()
        concurrency._retired_keys.clear()
        concurrency._retired_limits.clear()
        concurrency._deleted_retire_targets.clear()
    apikey_limiter._slots.clear()
    apikey_limiter._queued_body_bytes_by_key.clear()
    apikey_limiter._queued_body_spool_bytes_by_key.clear()
    apikey_limiter._active_body_guards.clear()
    with channel_state.mutation_lock:
        channel_state._transition_keys.clear()
        channel_state._aliases.clear()
        channel_state._deleted_keys.clear()
        channel_state._generation_targets.clear()
        channel_state._legacy_api_generations.clear()
        channel_state._legacy_oauth_generations.clear()
    ui._code_to_name.clear()


def _install_config(initial: dict[str, Any]) -> None:
    # Production composition registers these factories before Telegram starts.
    registry.register_channel_factory("openai-chat", OpenAIApiChannel)
    registry.register_channel_factory("openai-responses", OpenAIApiChannel)

    def mutate(cfg: dict[str, Any]) -> None:
        defaults: dict[str, Any] = {
            "channels": [],
            "apiKeys": {},
            "modelMapping": {"global": {}},
            "loadBalancing": {},
            "apiKeyLimiter": {
                "enabled": True,
                "defaultMaxConcurrent": 2,
                "defaultMaxQueue": 3,
                "defaultQueueWaitSeconds": 60,
            },
            "xaiOAuth": {"imageModels": [], "videoModels": []},
            "errorWindows": [1, 5, 15, 0],
            "errorGraceCount": 0,
        }
        for key in CONFIG_KEYS:
            cfg[key] = deepcopy(initial.get(key, defaults[key]))
        # Model-permission menus read OAuth models as well as API channels.
        # Accounts left by a prior lifecycle test are not part of this fixture.
        cfg["oauthAccounts"] = deepcopy(initial.get("oauthAccounts", []))
        # Quota rendering is shared by OAuth/channel menus but is outside this
        # segment's business snapshot. Pin its production default so preceding
        # OAuth tests cannot leak a progress-bar preference into strict traces.
        cfg["quotaProgressBar"] = bool(initial.get("quotaProgressBar", True))
        # This setting participates in generation retirement but is intentionally
        # outside the segment's business-state snapshot. Pin the production
        # default so a prior test cannot change the captured frozen limit.
        cfg["concurrency"] = deepcopy(initial.get("concurrency", {
            "enabled": True,
            "queueWaitSeconds": 30,
            "defaultMaxConcurrent": 0,
        }))
    config.update(mutate)
    registry.rebuild_from_config()


def _seed_runtime(runtime: dict[str, Any]) -> None:
    period = deepcopy(runtime.get("periodStats", _empty_period()))
    menu_cache.PERIOD_STATS.store(("period", int(MONTH_START)), period)
    menu_cache.HISTORY_TOTALS.store(
        "apikey-history", deepcopy(runtime.get("historyTotals", {})),
    )
    for row in runtime.get("channelModelStats", []):
        menu_cache.DETAIL_STATS.store(
            ("channel-model", row["channelKey"], int(MONTH_START)),
            deepcopy(row.get("rows", [])),
        )
    for row in runtime.get("apikeyModelStats", []):
        menu_cache.DETAIL_STATS.store(
            ("apikey-model", row["name"], int(MONTH_START)),
            deepcopy(row.get("rows", [])),
        )
    for item in runtime.get("cooldowns", []):
        cooldown.record_error(
            item["channelKey"], item["model"], item.get("message"),
            cooldown_until=item.get("cooldownUntil", int(FAKE_NOW * 1000) + 60_000),
        )
    for item in runtime.get("affinities", []):
        affinity.upsert(item["fingerprint"], item["channelKey"], item["model"])
    for item in runtime.get("clientAffinities", []):
        affinity.client_upsert(item["clientKey"], item["channelKey"], item["model"])
    for item in runtime.get("scorer", []):
        scorer.record_success(
            item["channelKey"], item["model"], item.get("connectMs", 10),
            item.get("firstTokenMs", 20), item.get("totalMs", 30),
        )


def _state_snapshot(chat_id: int = 42) -> dict[str, Any] | None:
    return json_value(states.get_state(chat_id))


def _config_snapshot() -> dict[str, Any]:
    cfg = config.get()
    return {key: json_value(deepcopy(cfg.get(key))) for key in CONFIG_KEYS}


def _registry_snapshot() -> list[dict[str, Any]]:
    rows = []
    for channel in registry.all_channels():
        if channel.type != "api":
            continue
        rows.append({
            "key": channel.key,
            "stateKey": getattr(channel, "state_key", channel.key),
            "name": channel.display_name,
            "protocol": getattr(channel, "protocol", "anthropic"),
            "enabled": channel.enabled,
            "models": json_value(deepcopy(channel.models)),
        })
    return rows


def _exception(exc: BaseException | None) -> dict[str, str] | None:
    if exc is None:
        return None
    return {"type": type(exc).__name__, "message": str(exc)}


def run_menu_case(case: dict[str, Any], domain: str, monkeypatch) -> dict[str, Any]:
    """Execute one manifest entry against the actual frozen menu module."""
    runtime = deepcopy(case["initialRuntime"])
    monkeypatch.setattr(states.time, "time", lambda: FAKE_NOW)
    monkeypatch.setattr(cooldown, "_now_ms", lambda: int(FAKE_NOW * 1000))
    monkeypatch.setattr(state_db, "now_ms", lambda: int(FAKE_NOW * 1000))
    monkeypatch.setattr(channel_menu, "_month_start_ts", lambda: MONTH_START)
    monkeypatch.setattr(apikey_menu, "_month_start_ts", lambda: MONTH_START)
    time_ns_counter = {"value": 1_700_000_000_000_000_000}
    def fake_time_ns() -> int:
        time_ns_counter["value"] += 1
        return time_ns_counter["value"]
    monkeypatch.setattr(channel_wizard.time, "time_ns", fake_time_ns)

    uuid_counter = {"value": 100}

    class FakeUuid:
        @property
        def hex(self) -> str:
            uuid_counter["value"] += 1
            return f"{uuid_counter['value']:032x}"

    # Patch only registry's module reference.  Mutating stdlib uuid.uuid4 here
    # also changes Codex identity generation and can persist a non-UUID repr.
    monkeypatch.setattr(registry, "uuid", SimpleNamespace(uuid4=lambda: FakeUuid()))
    monkeypatch.setattr(apikey_menu.secrets, "token_hex", lambda size: "ab" * size)
    monkeypatch.setattr(
        channel_menu.provider_usage, "_SECRET_CACHE", b"fixed-provider-usage-secret",
    )

    _clear_domain_state()
    _install_config(case["initialConfig"])
    # Direct deep-link callbacks in the manifest emulate buttons rendered on a
    # previous page, so restore the deterministic short-code registry first.
    for channel in registry.all_channels():
        if channel.type == "api":
            ui.register_code(channel.display_name)
    for key_name in (config.get().get("apiKeys") or {}):
        apikey_menu._short_of(key_name)
    _seed_runtime(runtime)
    initial_state = case["initialState"]
    if initial_state:
        states._states[42] = {
            "action": initial_state["action"],
            "data": deepcopy(initial_state.get("data", {})),
            "ts": FAKE_NOW - float(initial_state.get("ageSeconds", 0)),
        }

    transport = MenuTransport()
    monkeypatch.setattr(ui, "api", transport)
    ui.configure("fake-token-channels-apikey", [42])

    runtime_events: dict[str, Any] = {
        "providerSchedule": [],
        "viewBegins": [],
        "deleteDelays": [],
        "limiterForget": [],
        "probeCalls": [],
        "discoveryCalls": [],
        "providerCleanup": [],
        "retiredGenerations": [],
        "concurrencyRetire": [],
    }
    monkeypatch.setattr(
        menu_cache, "begin_view",
        lambda chat_id, message_id: runtime_events["viewBegins"].append([chat_id, message_id]),
    )

    schedule_results = list(runtime.get("providerScheduleResults", []))
    def schedule_provider(channel, **kwargs):
        runtime_events["providerSchedule"].append({
            "channelKey": channel.key,
            "kwargs": json_value(deepcopy(kwargs)),
        })
        return schedule_results.pop(0) if schedule_results else True
    monkeypatch.setattr(channel_menu.provider_usage, "schedule_refresh", schedule_provider)
    usage_by_name = runtime.get("providerUsage", {})
    monkeypatch.setattr(
        channel_menu.provider_usage, "cached",
        lambda channel: deepcopy(usage_by_name.get(
            channel.display_name, {"status": "missing", "snapshot": None},
        )),
    )
    original_provider_cleanup = channel_menu.provider_usage.cleanup_account_if_orphaned
    def provider_cleanup(account_id):
        runtime_events["providerCleanup"].append(account_id)
        return original_provider_cleanup(account_id)
    monkeypatch.setattr(
        channel_menu.provider_usage, "cleanup_account_if_orphaned", provider_cleanup,
    )

    original_retire_deleted = channel_state.retire_deleted
    def retire_deleted(channel_key):
        runtime_events["retiredGenerations"].append(channel_key)
        return original_retire_deleted(channel_key)
    monkeypatch.setattr(channel_state, "retire_deleted", retire_deleted)

    original_concurrency_retire = concurrency.retire_channel
    def retire_concurrency(channel_key, **kwargs):
        runtime_events["concurrencyRetire"].append({
            "channelKey": channel_key,
            "kwargs": json_value(deepcopy(kwargs)),
        })
        return original_concurrency_retire(channel_key, **kwargs)
    monkeypatch.setattr(concurrency, "retire_channel", retire_concurrency)

    async def fake_sleep(delay):
        runtime_events["deleteDelays"].append(delay)
    monkeypatch.setattr(channel_menu.asyncio, "sleep", fake_sleep)

    probe_results = runtime.get("probeResults", {})
    async def fake_probe(channel, model, progress_cb=None, **kwargs):
        runtime_events["probeCalls"].append({"channelKey": channel.key, "model": model})
        for line in runtime.get("probeProgress", []):
            if progress_cb is not None:
                await progress_cb(line)
        value = probe_results.get(model, runtime.get("defaultProbe", [True, 25, None]))
        if isinstance(value, dict) and "raise" in value:
            raise RuntimeError(value["raise"])
        return tuple(value)
    monkeypatch.setattr(channel_menu.probe, "probe_with_progress", fake_probe)
    channel_menu._SYNC_SPAWN = True

    async def fake_discover(url, api_key, **kwargs):
        runtime_events["discoveryCalls"].append({
            "url": url, "apiKey": api_key, "kwargs": json_value(deepcopy(kwargs)),
        })
        if runtime.get("discoveryError"):
            raise channel_wizard.ModelsDiscoveryError(runtime["discoveryError"])
        return list(runtime.get("discoveredModels", ["model-a", "model-b"]))
    monkeypatch.setattr(channel_wizard, "discover_models", fake_discover)

    original_forget = apikey_limiter.forget_key
    def forget(name):
        runtime_events["limiterForget"].append(name)
        return original_forget(name)
    monkeypatch.setattr(apikey_limiter, "forget_key", forget)

    state_steps: list[dict[str, Any]] = []
    original_set = states.set_state
    original_pop = states.pop_state
    current_step = {"value": -1}
    def traced_set(chat_id, action, data=None):
        result = original_set(chat_id, action, data)
        state_steps.append({
            "event": "set", "step": current_step["value"],
            "afterTgCall": len(transport.calls), "state": _state_snapshot(chat_id),
        })
        return result
    def traced_pop(chat_id):
        result = original_pop(chat_id)
        state_steps.append({
            "event": "pop", "step": current_step["value"],
            "afterTgCall": len(transport.calls), "popped": json_value(result),
            "state": _state_snapshot(chat_id),
        })
        return result
    monkeypatch.setattr(states, "set_state", traced_set)
    monkeypatch.setattr(states, "pop_state", traced_pop)

    menu = channel_menu if domain == "channel" else apikey_menu
    exception: BaseException | None = None
    for index, operation in enumerate(case["entry"]["steps"]):
        current_step["value"] = index
        handled: Any = None
        try:
            kind = operation["kind"]
            if kind == "callback":
                handled = menu.handle_callback(42, 100, operation.get("cbId", f"cb-{index}"), operation["data"])
            elif kind == "text":
                action = operation.get("action")
                if action is None:
                    state = states.get_state(42) or {}
                    action = state.get("action", "")
                handled = menu.handle_text_state(42, action, operation.get("text", ""))
            elif kind == "sendNew":
                menu.send_new(42, page=operation.get("page", 1))
                handled = True
            elif kind == "setState":
                states.set_state(42, operation["action"], deepcopy(operation.get("data", {})))
                handled = True
            elif kind == "expireState":
                if 42 in states._states:
                    states._states[42]["ts"] = FAKE_NOW - 601
                handled = True
            elif kind == "dropResource":
                resource = operation["resource"]
                if resource == "channel":
                    name = operation["name"]
                    config.update(lambda cfg: cfg.__setitem__(
                        "channels", [row for row in cfg.get("channels", []) if row.get("name") != name],
                    ))
                    registry.rebuild_from_config()
                elif resource == "apikey":
                    name = operation["name"]
                    config.update(lambda cfg: (cfg.get("apiKeys") or {}).pop(name, None))
                handled = True
            elif kind == "clearStatsCache":
                menu_cache.PERIOD_STATS.clear()
                menu_cache.DETAIL_STATS.clear()
                menu_cache.HISTORY_TOTALS.clear()
                handled = True
            else:
                raise AssertionError(f"unknown operation kind: {kind}")
        except BaseException as exc:  # expected exception is part of the trace
            exception = exc
        state_steps.append({
            "event": "checkpoint", "step": index, "kind": operation["kind"],
            "handled": handled, "tgCallCount": len(transport.calls),
            "state": _state_snapshot(),
        })
        if exception is not None:
            break

    final = {
        "config": _config_snapshot(),
        "registry": _registry_snapshot(),
        "cooldown": json_value(cooldown.snapshot()),
        "affinity": json_value(affinity.snapshot()),
        "clientAffinity": json_value(affinity.client_snapshot()),
        "scorer": json_value(scorer.snapshot()),
        "state": _state_snapshot(),
        "runtimeEvents": json_value(runtime_events),
        "limiterTotals": json_value(apikey_limiter.totals()),
        "retiredChannelKeys": sorted(channel_state._deleted_keys),
    }
    channel_menu._SYNC_SPAWN = False
    return {
        "caseId": case["caseId"],
        "capabilityId": case["capabilityId"],
        "entry": deepcopy(case["entry"]),
        "initialConfig": deepcopy(case["initialConfig"]),
        "initialState": deepcopy(case["initialState"]),
        "initialRuntime": deepcopy(case["initialRuntime"]),
        "tgApi": json_value(transport.calls),
        "stateSteps": json_value(state_steps),
        "finalBusinessState": final,
        "expectedException": _exception(exception),
    }
