"""Shared deterministic capture support for the v0.31.13 OAuth TG contract.

This module is intentionally named inside the package-owned test namespace.  It
contains no tests of its own; the three executable groups import it.  All
provider, Telegram and persistence boundaries are replaced with in-process
fakes before a menu function is invoked.
"""
from __future__ import annotations

import sys
from copy import deepcopy
from datetime import datetime as _DateTime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from src.tests import _isolation
_isolation.isolate()

from src import (
    affinity, config, cooldown, load_balancing, model_metadata, oauth_manager,
    state_db, status_monitor, update_checker,
)
from src.oauth import antigravity as antigravity_provider
from src.oauth import cursor as cursor_provider
from src.oauth import openai as openai_provider
from src.oauth import xai as xai_provider
from src.telegram import menu_cache, states, ui
from src.telegram.menus import oauth_account_models_menu as oam
from src.telegram.menus import oauth_menu as om
from src.tests.tg_contract import TraceCapture, assert_strict_equal, load_jsonl
from src.tests.tg_contract.current import load_current_additions

# v0.31.13 remains the immutable historical recording.  Current model-center
# deltas live in a sparse overlay so unaffected payload/state assertions still
# come byte-for-byte from that archive.
SEGMENT = Path(__file__).parent / "fixtures/tg_contract/v0.31.13/segments/oauth.jsonl"
CURRENT_OVERRIDES = (
    Path(__file__).parent
    / "fixtures/tg_contract/model-center-2026-09-13/overrides/oauth.jsonl"
)
ASSIGNED_IDS = frozenset({
    "TG-OA-01", "TG-OA-02", "TG-OA-03", "TG-OA-04", "TG-OA-05",
    "TG-OA-06", "TG-OA-07", "TG-ODM-01", "TG-OA-SET-01",
})
FAKE_NOW = 1_700_000_000.0


class FrozenDateTime(_DateTime):
    @classmethod
    def now(cls, tz=None):
        value = cls.fromtimestamp(FAKE_NOW, tz=timezone.utc)
        return value if tz is not None else value.replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return cls.fromtimestamp(FAKE_NOW, tz=timezone.utc).replace(tzinfo=None)


class ImmediateThread:
    """Thread fake recording the boundary while running deterministic work now."""
    events: list[dict[str, Any]] = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None, **_):
        self.target, self.args, self.kwargs = target, args, kwargs or {}
        self.daemon, self.name = daemon, name
        self.events.append({"event": "thread_created", "name": name, "daemon": daemon})

    def start(self):
        self.events.append({"event": "thread_started", "name": self.name})
        if self.target:
            self.target(*self.args, **self.kwargs)


class DeferredThread(ImmediateThread):
    """Thread fake proving a callback returns before provider work executes."""
    pending: list[Any] = []

    def start(self):
        self.events.append({"event": "thread_started", "name": self.name})
        self.pending.append((self.target, self.args, self.kwargs))


class FakeEnv:
    def __init__(self, case: dict[str, Any], monkeypatch):
        self.case = case
        self.monkeypatch = monkeypatch
        self.cfg = deepcopy(case.get("initialConfig") or {})
        self.cfg.setdefault("oauth", {})["mockMode"] = True
        self.cfg.setdefault("oauthAccounts", [])
        self.cfg.setdefault("oauthUsageDisplayMode", "used")
        self.cfg.setdefault("quotaProgressBar", True)
        self.cfg.setdefault("cchMode", "disabled")
        self.cfg.setdefault("quotaMonitor", {
            "enabled": False, "intervalSeconds": 60,
            "disableThresholdPercent": 95, "resumeThresholdPercent": 95,
        })
        self.runtime = deepcopy(case.get("initialRuntime") or {})
        self.events: list[Any] = []
        self.quota = deepcopy(self.runtime.get("quota") or {})
        self.cooldowns = deepcopy(self.runtime.get("cooldowns") or [])
        self.capture = TraceCapture({
            "sendMessage": {"ok": True, "result": {"message_id": 700}},
            "editMessageText": {"ok": True, "result": {}},
            "answerCallbackQuery": {"ok": True, "result": True},
            "deleteMessage": {"ok": True, "result": True},
            "getFile": {"ok": True, "result": {"file_path": "fake/import.json", "file_size": 2}},
        })
        self._install()

    def _install(self):
        mp = self.monkeypatch
        states.clear_all()
        ui._code_to_name.clear()
        ui.configure("fake-contract-token", [42])
        mp.setattr(states.time, "time", lambda: FAKE_NOW)
        mp.setattr(om.time, "time", lambda: FAKE_NOW)
        mp.setattr(oam.time, "time", lambda: FAKE_NOW)
        mp.setattr(om, "datetime", FrozenDateTime)
        mp.setattr(ui, "api", self.capture.api)
        mp.setattr(config, "get", lambda: self.cfg)
        mp.setattr(config, "update", self.config_update)
        mp.setattr(state_db, "quota_load", lambda key: deepcopy(self.quota.get(key)))
        mp.setattr(state_db, "quota_save", self.quota_save)
        mp.setattr(state_db, "quota_delete", lambda key: self.quota.pop(key, None))
        mp.setattr(state_db, "now_ms", lambda: int(FAKE_NOW * 1000))
        mp.setattr(cooldown, "active_entries", lambda: deepcopy(self.cooldowns))
        mp.setattr(cooldown, "get_state", self.cooldown_state)
        mp.setattr(cooldown, "clear", self.cooldown_clear)
        mp.setattr(cooldown, "clear_all", self.cooldown_clear_all)
        mp.setattr(affinity, "delete_by_channel", lambda key: self.events.append(["affinity_server_delete", key]))
        mp.setattr(affinity, "client_delete_by_channel", lambda key: self.events.append(["affinity_client_delete", key]))
        mp.setattr(load_balancing, "is_initialized", lambda: False)
        mp.setattr(status_monitor, "get_active_summary", lambda: None)
        mp.setattr(update_checker, "get_update_banner", lambda: None)
        mp.setattr(om, "_converge_cached_quota_state", lambda: None)
        mp.setattr(om, "_list_snapshot_ready", lambda: True)
        mp.setattr(om, "_render_cached_list", lambda page, filt: om._list_text_and_kb(
            page, filt, month_snapshot={"by_channel": {}}, stats_loading=False,
        ))
        mp.setattr(menu_cache, "begin_view", lambda chat_id, message_id: self.events.append(
            ["begin_view", chat_id, message_id]
        ))
        mp.setattr(om, "_request_window_snapshots", lambda accounts, **kwargs: True)
        mp.setattr(om, "_schedule_openai_metadata_for_ui", lambda *a, **k: None)
        mp.setattr(om, "_schedule_oauth_cache_refresh_for_ui", lambda *a, **k: None)
        mp.setattr(om, "_account_period_stats", lambda *a, **k: None)
        mp.setattr(om, "_queue_oauth_detail_stats", lambda key, **kwargs: True)
        mp.setattr(om, "_render_cached_detail", lambda key, page, filt, **kw: om._detail_text_and_kb(
            key, page=page, filter_key=filt, month_snapshot={}, model_stats=[]
        ))
        mp.setattr(oauth_manager, "evaluate_and_toggle_by_cached_quota", lambda key: None)
        mp.setattr(om.threading, "Thread", ImmediateThread)
        ImmediateThread.events = []
        DeferredThread.events = []
        DeferredThread.pending = []

    def config_update(self, mutator):
        mutator(self.cfg)
        self.events.append(["config_update", self.public_config()])
        return self.cfg

    def quota_save(self, key, usage, **kwargs):
        row = deepcopy(usage)
        row.update(kwargs)
        self.quota[key] = row
        self.events.append(["quota_save", key, deepcopy(row)])

    def cooldown_state(self, channel_key, model):
        for row in self.cooldowns:
            if row.get("channel_key") == channel_key and row.get("model") == model:
                return deepcopy(row)
        return None

    def cooldown_clear(self, channel_key, model=None):
        before = len(self.cooldowns)
        self.cooldowns[:] = [row for row in self.cooldowns if not (
            row.get("channel_key") == channel_key and (model is None or row.get("model") == model)
        )]
        count = before - len(self.cooldowns)
        self.events.append(["cooldown_clear", channel_key, model, count])
        return count

    def cooldown_clear_all(self):
        count = len(self.cooldowns)
        self.cooldowns.clear()
        self.events.append(["cooldown_clear_all", count])
        return count

    def account(self, provider="claude", index=1, **extra):
        email = extra.pop("email", f"{provider}{index}@fake.invalid")
        account = {
            "email": email,
            "provider": provider,
            "type": provider,
            "access_token": f"fake-access-{provider}-{index}",
            "refresh_token": f"fake-refresh-{provider}-{index}",
            "expired": "2030-01-02T03:04:05Z",
            "last_refresh": "2029-12-01T00:00:00Z",
            "enabled": True,
            "disabled_reason": None,
            "disabled_until": None,
            "models": [f"{provider}-model-{n}" for n in range(1, 9)],
        }
        if provider == "openai":
            account.update({"workspace_id": f"workspace-{index}", "chatgpt_account_id": f"workspace-{index}", "plan_type": "plus"})
        elif provider == "xai":
            account.update({"subject": f"xai-subject-{index}", "plan_type": "Premium"})
        elif provider == "cursor":
            account.update({"subject": f"cursor-subject-{index}", "label": f"Cursor Fake {index}", "plan_type": "Pro"})
        elif provider == "antigravity":
            account.update({"project_id": f"project-{index}", "plan_type": "Credits"})
        account.update(extra)
        return account

    def seed_accounts(self, providers=None, count=None):
        if self.cfg["oauthAccounts"]:
            return
        providers = providers or ["claude", "openai", "xai", "cursor", "antigravity"]
        if count is None:
            count = len(providers)
        self.cfg["oauthAccounts"] = [self.account(providers[i % len(providers)], i + 1) for i in range(count)]

    def key(self, index=0):
        return oauth_manager.get_account_key(self.cfg["oauthAccounts"][index])

    def short(self, index=0):
        return ui.register_code(self.key(index))

    def public_config(self):
        keep = {
            "oauthUsageDisplayMode", "quotaProgressBar", "cchMode", "quotaMonitor",
            "oauthDefaultModels", "openaiOAuth", "xaiOAuth", "antigravityOAuth",
            "apiKeys", "modelMapping", "ingressDefaultModel", "images",
        }
        out = {key: deepcopy(value) for key, value in self.cfg.items() if key in keep}
        accounts = []
        for item in self.cfg.get("oauthAccounts") or []:
            accounts.append({key: deepcopy(value) for key, value in item.items() if key not in {
                "access_token", "refresh_token", "id_token",
            }})
        out["oauthAccounts"] = accounts
        return out

    def state_snapshot(self, label="after"):
        return {"after": label, "chatId": 42, "state": deepcopy(states.get_state(42))}

    def final(self, **extra):
        value = {
            "config": self.public_config(),
            "quota": deepcopy(self.quota),
            "cooldowns": deepcopy(self.cooldowns),
            "events": deepcopy(self.events),
        }
        value.update(deepcopy(extra))
        return value


def actual(case, env: FakeEnv, *, state_steps=None, final=None, exception=None):
    return {
        "caseId": case["caseId"],
        "capabilityId": case["capabilityId"],
        "entry": deepcopy(case["entry"]),
        "initialConfig": deepcopy(case["initialConfig"]),
        "initialState": deepcopy(case["initialState"]),
        "initialRuntime": deepcopy(case["initialRuntime"]),
        "tgApi": deepcopy(env.capture.calls),
        "stateSteps": deepcopy(state_steps or []),
        "finalBusinessState": deepcopy(final if final is not None else env.final()),
        "expectedException": deepcopy(exception),
    }


def invoke_safely(callable_obj, *args, **kwargs):
    try:
        result = callable_obj(*args, **kwargs)
        return result, None
    except Exception as exc:  # exception is itself frozen contract output
        return None, {"type": type(exc).__name__, "message": str(exc)}


def check_trace(case, observed):
    """Strict replay only: the committed baseline has no snapshot-update path."""
    assert_strict_equal(case, observed)


def _current_cases():
    archived = load_jsonl(SEGMENT)
    overrides = load_jsonl(CURRENT_OVERRIDES) if CURRENT_OVERRIDES.exists() else []
    archived_ids = {case["caseId"] for case in archived}
    override_map = {case["caseId"]: case for case in overrides}
    if len(override_map) != len(overrides):
        raise AssertionError("duplicate OAuth contract override caseId")
    unknown = set(override_map) - archived_ids
    if unknown:
        raise AssertionError(f"unknown OAuth contract overrides: {sorted(unknown)}")
    result = [override_map.get(case["caseId"], case) for case in archived]
    return result + load_current_additions(SEGMENT, result)


def cases_for(*ids):
    return [case for case in _current_cases() if case["capabilityId"] in ids]
