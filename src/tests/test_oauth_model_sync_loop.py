from __future__ import annotations

import asyncio
import copy
import concurrent.futures
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import config, oauth_manager
from src.telegram.menus import oauth_menu


def _iso(delta_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _account(provider: str, index: int, **extra) -> dict:
    item = {
        "provider": provider, "type": provider, "email": f"{provider}{index}@x",
        "access_token": f"t{index}", "refresh_token": f"r{index}",
        "expired": "2999-01-01T00:00:00Z", "models": [],
    }
    if provider == "openai": item["workspace_id"] = f"ws{index}"
    if provider == "xai": item["subject"] = f"sub{index}"
    if provider == "antigravity": item["project_id"] = f"p{index}"
    if provider == "cursor": item.update(subject=f"cur{index}", cursor_disabled_models=[])
    item.update(extra)
    return item


@pytest.fixture
def sync_config():
    before = copy.deepcopy(config.get())
    config.update(lambda cfg: cfg.update(oauthAccounts=[]))
    yield
    config.update(lambda cfg: (cfg.clear(), cfg.update(before)))


def test_due_requires_complete_provider_native_catalog():
    now = datetime.now(timezone.utc)
    fresh = {"models": ["a", "b"], "last_model_sync": _iso(-60)}

    assert oauth_manager._model_sync_due(_account("openai", 1, **fresh), now=now)
    assert oauth_manager._model_sync_due(_account(
        "openai", 1, **fresh,
        account_model_catalog={"models": [{"id": "a"}]},
    ), now=now)
    assert not oauth_manager._model_sync_due(_account(
        "openai", 1, **fresh,
        account_model_catalog={"models": [{"id": "a"}, {"id": "b", "name": "B"}]},
    ), now=now)

    assert oauth_manager._model_sync_due(_account("cursor", 1, **fresh), now=now)
    assert oauth_manager._model_sync_due(_account(
        "cursor", 1, **fresh,
        cursor_model_catalog={"models": [{"id": "a"}, None, {"name": "invalid"}]},
    ), now=now)


def test_due_empty_stale_and_failed_retry():
    now = datetime.now(timezone.utc)
    assert oauth_manager._model_sync_due(_account("claude", 1), now=now)
    complete = {"account_model_catalog": {"models": [{"id": "a"}]}}
    assert oauth_manager._model_sync_due(
        _account("claude", 1, models=["a"], last_model_sync=_iso(-(6 * 3600 + 1)), **complete),
        now=now,
    )
    recent_failure = _account("claude", 1, last_model_sync_attempt=_iso(-60), last_model_sync_error="timeout")
    old_failure = _account("claude", 1, last_model_sync_attempt=_iso(-(15 * 60 + 1)), last_model_sync_error="timeout")
    assert not oauth_manager._model_sync_due(recent_failure, now=now)
    assert oauth_manager._model_sync_due(old_failure, now=now)


@pytest.mark.asyncio
async def test_metadata_only_backfill_does_not_notify_or_repeat_when_fresh(monkeypatch, sync_config):
    account = _account("openai", 1, models=["a", "b"], last_model_sync=_iso(-60))
    config.update(lambda cfg: cfg.update(oauthAccounts=[account]))
    monkeypatch.setattr(oauth_manager, "mock_mode_enabled", lambda: False)

    async def token(key):
        return "t"

    calls = []

    def discover(account, timeout):
        calls.append(account)
        return type("R", (), {
            "models": ["a", "b"],
            "catalog": {"schema": 1, "models": [{"id": "a"}, {"id": "b", "name": "B"}]},
            "source": "upstream:test",
        })()

    sent = []
    monkeypatch.setattr(oauth_manager, "ensure_valid_token", token)
    monkeypatch.setattr(oauth_manager.oauth_model_discovery, "discover", discover)
    monkeypatch.setattr(oauth_manager.notifier, "notify", sent.append)

    first = await oauth_manager.oauth_model_sync_once(trigger="background")
    assert len(first) == 1
    assert first[0]["old_model_ids"] == first[0]["new_model_ids"] == ["a", "b"]
    assert first[0]["changed"] is False
    assert sent == []

    saved = oauth_manager.get_account("openai:openai1@x:ws1")
    assert saved["account_model_catalog"]["models"][1]["name"] == "B"
    assert not oauth_manager._model_sync_due(saved)
    assert await oauth_manager.oauth_model_sync_once(trigger="background") == []
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unified_once_has_five_providers_and_concurrency_three(monkeypatch, sync_config):
    accounts = [_account(p, i) for i, p in enumerate(("claude", "openai", "xai", "antigravity", "cursor"), 1)]
    config.update(lambda cfg: cfg.update(oauthAccounts=accounts))
    active = peak = 0
    seen = []
    gate = asyncio.Event()

    async def refresh(key, *, timeout_s):
        nonlocal active, peak
        seen.append(key.split(":", 1)[0])
        active += 1; peak = max(peak, active)
        if len(seen) >= 3: gate.set()
        await gate.wait()
        await asyncio.sleep(0)
        active -= 1
        return {"action": "updated", "account_key": key, "models": 1, "had_success_baseline": False, "changed": True}

    monkeypatch.setattr(oauth_manager, "refresh_account_models", refresh)
    out = await oauth_manager.oauth_model_sync_once(notify_changes=False)
    assert len(out) == 5
    assert set(seen) == {"claude", "openai", "xai", "antigravity", "cursor"}
    assert peak == 3


def test_server_registers_only_unified_loop():
    text = Path("server.py").read_text()
    assert "create_task(oauth_manager.oauth_model_sync_loop())" in text
    assert "create_task(oauth_manager.cursor_model_sync_loop())" not in text


@pytest.mark.asyncio
async def test_background_notification_matrix_and_failure_isolated(monkeypatch, sync_config):
    account = _account("openai", 1, models=["a", "b"], last_model_sync=_iso(-7 * 3600), label="A < B")
    config.update(lambda cfg: cfg.update(oauthAccounts=[account]))
    sent = []

    async def changed(key, *, timeout_s):
        return {"action": "updated", "account_key": key, "models": 2,
                "had_success_baseline": True, "changed": True,
                "added": ["c<script>"], "removed": ["b"]}
    monkeypatch.setattr(oauth_manager, "refresh_account_models", changed)
    monkeypatch.setattr(oauth_manager.notifier, "notify", sent.append)
    await oauth_manager.oauth_model_sync_once(force=True, trigger="background")
    assert len(sent) == 1 and "新增 1" in sent[0] and "删除 1" in sent[0]
    assert "A &lt; B" in sent[0] and "c&lt;script&gt;" in sent[0]

    sent.clear()
    await oauth_manager.oauth_model_sync_once(force=True, trigger="manual")
    assert sent == []

    async def first(key, *, timeout_s):
        return {"action": "updated", "account_key": key, "models": 2,
                "had_success_baseline": False, "changed": True, "added": ["a"], "removed": []}
    monkeypatch.setattr(oauth_manager, "refresh_account_models", first)
    await oauth_manager.oauth_model_sync_once(force=True, trigger="background")
    assert sent == []

    monkeypatch.setattr(oauth_manager, "refresh_account_models", changed)
    monkeypatch.setattr(oauth_manager.notifier, "notify", lambda text: (_ for _ in ()).throw(RuntimeError("tg down")))
    out = await oauth_manager.oauth_model_sync_once(force=True, trigger="background")
    assert out[0]["action"] == "updated"


def test_notification_long_lists_are_bounded_and_escaped():
    values = [f"m<{i}>" for i in range(15)]
    text = oauth_manager._format_model_change_notification(
        _account("claude", 1, label="<admin>"), {"added": values, "removed": values},
    )
    assert text.count("另有 5 项") == 2
    assert "m&lt;0&gt;" in text and "m&lt;10&gt;" not in text and "&lt;admin&gt;" in text


@pytest.mark.asyncio
async def test_non_cursor_disabled_survives_remove_and_reappear(monkeypatch, sync_config):
    account = _account("openai", 1, models=["a", "b"], disabledModels=["b"], last_model_sync=_iso(-7 * 3600))
    config.update(lambda cfg: cfg.update(oauthAccounts=[account]))
    monkeypatch.setattr(oauth_manager, "mock_mode_enabled", lambda: False)
    async def token(key): return "t"
    monkeypatch.setattr(oauth_manager, "ensure_valid_token", token)
    catalogs = iter((["a", "c"], ["b", "c"]))
    monkeypatch.setattr(oauth_manager.oauth_model_discovery, "discover",
                        lambda account, timeout: type("R", (), {"models": next(catalogs), "source": "upstream:test"})())
    await oauth_manager.refresh_account_models("openai:openai1@x:ws1")
    saved = oauth_manager.get_account("openai:openai1@x:ws1")
    assert saved["models"] == ["a", "c"] and saved["disabledModels"] == ["b"]
    await oauth_manager.refresh_account_models("openai:openai1@x:ws1")
    saved = oauth_manager.get_account("openai:openai1@x:ws1")
    assert saved["models"] == ["b", "c"] and saved["disabledModels"] == ["b"]
    assert "b" not in oauth_manager.account_model_selection(saved)["effective_models"]


@pytest.mark.asyncio
async def test_cursor_disabled_survives_remove_and_reappear(monkeypatch, sync_config):
    account = _account(
        "cursor", 1, models=["a", "b"], cursor_disabled_models=["b"],
        last_model_sync=_iso(-7 * 3600),
    )
    config.update(lambda cfg: cfg.update(oauthAccounts=[account]))
    monkeypatch.setattr(oauth_manager, "mock_mode_enabled", lambda: False)
    async def token(key): return "t"
    monkeypatch.setattr(oauth_manager, "ensure_valid_token", token)
    catalogs = iter((
        {"models": [{"id": "a"}, {"id": "c"}], "fetched_at": _iso()},
        {"models": [{"id": "b"}, {"id": "c"}], "fetched_at": _iso()},
    ))
    monkeypatch.setattr(oauth_manager.cursor_provider, "fetch_model_catalog_sync", lambda token: next(catalogs))
    monkeypatch.setattr(oauth_manager.cursor_provider, "fetch_profile_sync", lambda token, account_key: {})
    await oauth_manager.refresh_account_models("cursor:cur1")
    saved = oauth_manager.get_account("cursor:cur1")
    assert saved["models"] == ["a", "c"] and saved["cursor_disabled_models"] == ["b"]
    await oauth_manager.refresh_account_models("cursor:cur1")
    saved = oauth_manager.get_account("cursor:cur1")
    assert saved["models"] == ["b", "c"] and saved["cursor_disabled_models"] == ["b"]
    assert oauth_manager.cursor_disabled_models(saved) == {"b"}


@pytest.mark.parametrize("provider", ["claude", "openai", "xai", "antigravity", "cursor"])
@pytest.mark.asyncio
async def test_token_refresh_failure_persists_backoff_without_leaking_secret(
    monkeypatch, sync_config, provider,
):
    account = _account(provider, 1, models=["lkg"], disabledModels=["hidden"])
    if provider == "cursor":
        account["cursor_disabled_models"] = ["hidden"]
    config.update(lambda cfg: cfg.update(oauthAccounts=[account]))
    key = oauth_manager.get_account_key(account)
    secret = account["access_token"]

    async def fail(_key):
        raise RuntimeError(f"proxy failed with bearer {secret}")

    monkeypatch.setattr(oauth_manager, "ensure_valid_token", fail)
    result = await oauth_manager.refresh_account_models(key)
    saved = oauth_manager.get_account(key)
    assert result["action"] == "error"
    assert secret not in result.get("error", "")
    assert secret not in saved["last_model_sync_error"]
    assert saved["models"] == ["lkg"]
    assert oauth_manager.account_disabled_models(saved) == {"hidden"}
    assert not oauth_manager._model_sync_due(saved)


@pytest.mark.asyncio
async def test_successful_token_refresh_uses_fresh_generation(monkeypatch, sync_config):
    account = _account("openai", 1, models=["lkg"])
    config.update(lambda cfg: cfg.update(oauthAccounts=[account]))
    key = oauth_manager.get_account_key(account)

    async def refresh(_key):
        config.update(lambda cfg: cfg["oauthAccounts"][0].update(access_token="fresh-token"))
        return "fresh-token"

    def discover(snapshot, timeout):
        assert snapshot["access_token"] == "fresh-token"
        return type("R", (), {
            "models": ["fresh-model"], "catalog": {"models": [{"id": "fresh-model"}]},
            "source": "upstream:test",
        })()

    monkeypatch.setattr(oauth_manager, "mock_mode_enabled", lambda: False)
    monkeypatch.setattr(oauth_manager, "ensure_valid_token", refresh)
    monkeypatch.setattr(oauth_manager.oauth_model_discovery, "discover", discover)
    result = await oauth_manager.refresh_account_models(key)
    saved = oauth_manager.get_account(key)
    assert result["action"] == "updated"
    assert saved["access_token"] == "fresh-token" and saved["models"] == ["fresh-model"]


@pytest.mark.parametrize("provider", ["claude", "openai", "xai", "antigravity"])
def test_all_non_cursor_batch_edits_preserve_hidden_disabled(sync_config, provider):
    account = _account(provider, 1, models=["a", "b", "new"])
    account["disabledModels"] = ["a", "hidden", "new"]
    config.update(lambda cfg: cfg.update(oauthAccounts=[account]))
    key = oauth_manager.get_account_key(account)
    saved = oauth_manager.set_account_disabled_models(
        key, ["b"], visible_models=["a", "b"],
    )
    assert saved == {"b", "hidden", "new"}


def test_cursor_batch_edit_preserves_hidden_and_rejects_outside_snapshot(sync_config):
    account = _account(
        "cursor", 1, models=["a", "b", "new"],
        cursor_model_catalog={"models": [{"id": "a"}, {"id": "b"}, {"id": "new"}]},
        cursor_disabled_models=["a", "hidden", "new"],
    )
    config.update(lambda cfg: cfg.update(oauthAccounts=[account]))
    saved = oauth_manager.set_cursor_disabled_models(
        "cursor:cur1", ["b"], visible_models=["a", "b"],
    )
    assert saved == {"b", "hidden", "new"}
    with pytest.raises(ValueError, match="not found"):
        oauth_manager.set_cursor_disabled_models(
            "cursor:cur1", ["new"], visible_models=["a", "b"],
        )


@pytest.mark.asyncio
async def test_real_network_workers_never_exceed_global_three(monkeypatch, sync_config):
    accounts = [_account("openai", i) for i in range(1, 8)]
    config.update(lambda cfg: cfg.update(oauthAccounts=accounts))
    monkeypatch.setattr(oauth_manager, "mock_mode_enabled", lambda: False)
    async def token(_key): return "t"
    monkeypatch.setattr(oauth_manager, "ensure_valid_token", token)
    lock = threading.Lock()
    release = threading.Event()
    active = peak = started = 0

    def discover(account, timeout):
        nonlocal active, peak, started
        with lock:
            active += 1; started += 1; peak = max(peak, active)
        release.wait(2)
        with lock:
            active -= 1
        return type("R", (), {
            "models": ["m"], "catalog": {"models": [{"id": "m"}]},
            "source": "upstream:test",
        })()

    monkeypatch.setattr(oauth_manager.oauth_model_discovery, "discover", discover)
    task = asyncio.create_task(oauth_manager.oauth_model_sync_once(force=True, notify_changes=False))
    for _ in range(100):
        with lock:
            if started >= 3: break
        await asyncio.sleep(0.005)
    with lock:
        assert started == 3 and peak == 3
    release.set()
    results = await task
    assert len(results) == 7 and peak == 3


@pytest.mark.asyncio
async def test_slow_uncancellable_worker_has_no_late_catalog_write(monkeypatch, sync_config):
    account = _account("openai", 1, models=["lkg"], disabledModels=["hidden"])
    config.update(lambda cfg: cfg.update(oauthAccounts=[account]))
    monkeypatch.setattr(oauth_manager, "mock_mode_enabled", lambda: False)
    async def token(_key): return "t"
    monkeypatch.setattr(oauth_manager, "ensure_valid_token", token)

    def slow(_account, timeout):
        time.sleep(0.03)
        return type("R", (), {
            "models": ["late"], "catalog": {"models": [{"id": "late"}]},
            "source": "upstream:test",
        })()

    monkeypatch.setattr(oauth_manager.oauth_model_discovery, "discover", slow)
    result = await oauth_manager.refresh_account_models(
        "openai:openai1@x:ws1", timeout_s=0.005,
    )
    assert result["action"] == "timeout"
    assert oauth_manager.get_account("openai:openai1@x:ws1")["models"] == ["lkg"]
    await asyncio.sleep(0.04)
    assert oauth_manager.get_account("openai:openai1@x:ws1")["models"] == ["lkg"]


def test_foreground_timeout_does_not_cancel_and_uses_accurate_source(monkeypatch, sync_config):
    account = _account("openai", 1, models=["lkg"], disabledModels=[])
    config.update(lambda cfg: cfg.update(oauthAccounts=[account]))
    future = concurrent.futures.Future()
    monkeypatch.setattr(oauth_manager, "start_account_model_refresh", lambda key: future)
    monkeypatch.setattr(oauth_manager, "OAUTH_MODEL_SYNC_FOREGROUND_TIMEOUT_SECONDS", 0.001)
    calls = []
    finished = __import__("threading").Event()
    monkeypatch.setattr(oauth_menu.ui, "send", lambda *a, **k: {"result": {"message_id": 9}})
    def edit(*args, **kwargs):
        calls.append(args[2])
        if "任务将在后台继续" in args[2]: finished.set()
    monkeypatch.setattr(oauth_menu.ui, "edit", edit)
    result = oauth_menu._foreground_account_model_sync(
        1, "openai:openai1@x:ws1", provider="openai", label="user",
    )
    assert result["action"] == "started"
    assert finished.wait(1)
    assert not future.cancelled() and not future.done()
    assert "任务将在后台继续" in calls[-1] and "继续使用上次账户模型" in calls[-1]


def test_foreground_success_and_failure_copy(monkeypatch, sync_config):
    account = _account("openai", 1, models=[])
    config.update(lambda cfg: cfg.update(oauthAccounts=[account]))
    edits = []
    finished = __import__("threading").Event()
    monkeypatch.setattr(oauth_menu.ui, "send", lambda *a, **k: {"result": {"message_id": 8}})
    def edit(*args, **kwargs):
        edits.append(args[2])
        if "已同步模型" in args[2] or "后台将静默重试" in args[2]: finished.set()
    monkeypatch.setattr(oauth_menu.ui, "edit", edit)
    success = concurrent.futures.Future(); success.set_result({"action": "updated", "models": 2})
    monkeypatch.setattr(oauth_manager, "start_account_model_refresh", lambda key: success)
    oauth_menu._foreground_account_model_sync(1, "openai:openai1@x:ws1", provider="openai", label="u")
    assert finished.wait(1)
    assert "已同步模型" in edits[-1]
    finished.clear()
    failure = concurrent.futures.Future(); failure.set_result({"action": "error", "error": "safe failure"})
    monkeypatch.setattr(oauth_manager, "start_account_model_refresh", lambda key: failure)
    oauth_menu._foreground_account_model_sync(1, "openai:openai1@x:ws1", provider="openai", label="u")
    assert finished.wait(1)
    assert "后台将静默重试，当前使用默认模型" in edits[-1]
