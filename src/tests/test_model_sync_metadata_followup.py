"""Batch-tail metadata refresh: actual deltas, one fetch, and preserved LKG."""
from __future__ import annotations

import asyncio
import copy
from threading import Event

import httpx
import pytest

from src import config, model_metadata, model_pricing, oauth_manager
from src.oauth.workbuddy import common as workbuddy_common
from src.model_pricing import refresh_metadata_after_model_sync as real_metadata_followup
from src.telegram.menus import model_center_sync
from src.tests.test_oauth_model_sync_loop import sync_config, _account, _iso
from src.tests.test_model_center_upstream_sync import env, _api, _oauth, _install, _payload, _start, _wait


@pytest.mark.asyncio
async def test_workbuddy_uses_real_discovery_and_six_hour_cadence(monkeypatch, sync_config):
    account = _account("workbuddy", 1, disabledModels=["m"])
    config.update(lambda cfg: cfg.update(oauthAccounts=[account]))
    monkeypatch.setattr(oauth_manager, "mock_mode_enabled", lambda: False)
    async def token(key):
        return "fixture"
    monkeypatch.setattr(oauth_manager, "ensure_valid_token", token)
    calls = []
    def request(account, path, **kwargs):
        calls.append(path)
        return {"models": [{"id": "m", "name": "Model", "maxInputTokens": 128000}],
                "agents": [{"name": "cli", "models": ["m"]}]}
    monkeypatch.setattr(workbuddy_common, "request", request)
    metadata_calls = []
    async def metadata():
        saved = oauth_manager.get_account(oauth_manager.get_account_key(account))
        assert saved["models"] == ["m"]
        metadata_calls.append(True)
        return {"status": "succeeded"}
    monkeypatch.setattr(model_pricing, "refresh_metadata_after_model_sync", metadata)
    result = await oauth_manager.oauth_model_sync_once(notify_changes=False)
    assert len(result) == 1 and result[0]["catalog_changed"]
    saved = oauth_manager.get_account(oauth_manager.get_account_key(account))
    assert saved["disabledModels"] == ["m"]
    assert saved["account_model_catalog"]["models"][0]["maxInputTokens"] == 128000
    assert not oauth_manager._model_sync_due(saved)
    assert oauth_manager._model_sync_due({**saved, "last_model_sync": _iso(-6 * 3600 - 1)})
    assert await oauth_manager.oauth_model_sync_once(notify_changes=False) == []
    assert calls == ["/console/enterprises/personal/models"] and len(metadata_calls) == 1


@pytest.mark.parametrize("results,expected", [
    ([{"action": "updated", "changed": True}, {"action": "updated", "changed": True}], 1),
    ([{"action": "updated", "catalog_changed": True}, {"action": "error"}], 1),
    ([{"action": "updated", "changed": False}, {"action": "not_modified"}], 0),
    ([{"action": "error", "changed": True}, {"action": "stale", "catalog_changed": True}], 0),
])
@pytest.mark.asyncio
async def test_auto_batch_waits_for_every_source_then_refreshes_once(monkeypatch, sync_config, results, expected):
    accounts = [_account("workbuddy", 1), _account("claude", 2)]
    config.update(lambda cfg: cfg.update(oauthAccounts=accounts))
    keys = [oauth_manager.get_account_key(a) for a in accounts]
    finished = []
    metadata_calls = []
    async def refresh(key, **kwargs):
        await asyncio.sleep(0)
        finished.append(key)
        return {"account_key": key, **results[keys.index(key)]}
    async def metadata():
        assert set(finished) == set(keys)
        metadata_calls.append(True)
        return {"status": "succeeded"}
    monkeypatch.setattr(oauth_manager, "refresh_account_models", refresh)
    monkeypatch.setattr(model_pricing, "refresh_metadata_after_model_sync", metadata)
    assert len(await oauth_manager.oauth_model_sync_once(notify_changes=False)) == 2
    assert len(metadata_calls) == expected


@pytest.mark.parametrize("provider", ["cursor", "workbuddy"])
def test_delta_ignores_timestamp_and_order_but_detects_metadata(monkeypatch, provider):
    field = "cursor_model_catalog" if provider == "cursor" else "account_model_catalog"
    before = _account(provider, 1, models=["a", "b"], **{field: {
        "fetched_at": "old", "models": [{"id": "a", "maxOutputTokens": 100}, {"id": "b"}],
    }})
    after = copy.deepcopy(before)
    after["models"].reverse()
    after[field]["fetched_at"] = "new"
    after[field]["models"].reverse()
    monkeypatch.setattr(oauth_manager, "get_account", lambda key: after)
    result = oauth_manager._normalize_model_refresh_result("test", before, {"action": "updated"})
    assert not result["catalog_changed"] and not result["changed"]
    after[field]["models"][1]["maxOutputTokens"] = 200
    result = oauth_manager._normalize_model_refresh_result("test", before, {"action": "updated"})
    assert result["catalog_changed"] and not result["changed"]
    result = oauth_manager._normalize_model_refresh_result("test", before, {"action": "stale"})
    assert not result["catalog_changed"]


@pytest.mark.parametrize("remote_failure,match_failure", [(False, False), (True, False), (False, True)])
@pytest.mark.asyncio
async def test_metadata_followup_download_then_reconcile_or_keep_local(monkeypatch, remote_failure, match_failure):
    monkeypatch.setattr(model_pricing.config, "get", lambda: {"pricing": {"enabled": True, "autoUpdate": True}})
    calls = []
    def download():
        calls.append("download")
        if remote_failure:
            raise OSError("offline")
        return True
    def match():
        calls.append("match")
        if match_failure:
            raise RuntimeError("match failed")
        return {"scanned": 5}
    monkeypatch.setattr(model_pricing, "refresh_remote_catalog_sync", download)
    monkeypatch.setattr(model_metadata, "auto_sync_metadata", match)
    result = await model_pricing.refresh_metadata_after_model_sync()
    assert calls == ["download", "match"]
    assert result["status"] == ("failed" if match_failure else "partial_failed" if remote_failure else "succeeded")
    assert result["catalog"] == ("local" if remote_failure else "updated")


@pytest.mark.asyncio
async def test_metadata_followup_respects_disabled_auto_update(monkeypatch):
    monkeypatch.setattr(model_pricing.config, "get", lambda: {"pricing": {"autoUpdate": False}})
    def forbidden():
        pytest.fail("autoUpdate=False must not download or reconcile")
    monkeypatch.setattr(model_pricing, "refresh_remote_catalog_sync", forbidden)
    monkeypatch.setattr(model_metadata, "auto_sync_metadata", forbidden)
    assert (await model_pricing.refresh_metadata_after_model_sync())["status"] == "skipped"


def test_manual_batch_refreshes_once_after_successes_and_failures(env, monkeypatch):
    _install([_api()], [_oauth("good"), _oauth("bad")])
    env.responses.update({
        "api-secret-alpha": _payload("new-api"),
        "oauth-secret-good": _payload("new-oauth"),
        "oauth-secret-bad": lambda request: httpx.Response(500),
    })
    calls = []
    async def metadata():
        assert env.requests == ["api-secret-alpha", "oauth-secret-good", "oauth-secret-bad"]
        assert config.get()["channels"][0]["models"][-1]["real"] == "new-api"
        assert config.get()["oauthAccounts"][0]["models"] == ["new-oauth"]
        calls.append(True)
        return {"status": "succeeded", "catalog": "updated"}
    monkeypatch.setattr(model_pricing, "refresh_metadata_after_model_sync", metadata)
    result = _wait(env, _start(env)).result
    assert len(calls) == 1 and result["metadataSync"]["status"] == "succeeded"
    assert result["status"] == "partial_failed"


def test_manual_unchanged_batch_does_not_refresh_metadata(env, monkeypatch):
    _install([_api()])
    env.responses["api-secret-alpha"] = _payload("old-real", "missing-today")
    async def forbidden():
        pytest.fail("unchanged batch must not refresh metadata")
    monkeypatch.setattr(model_pricing, "refresh_metadata_after_model_sync", forbidden)
    result = _wait(env, _start(env)).result
    assert result["status"] == "succeeded" and "metadataSync" not in result


def test_manual_metadata_failure_preserves_models_and_reports_partial(env, monkeypatch):
    _install([_api()])
    env.responses["api-secret-alpha"] = _payload("new-api")
    async def metadata():
        return {"status": "failed", "catalog": "local"}
    monkeypatch.setattr(model_pricing, "refresh_metadata_after_model_sync", metadata)
    result = _wait(env, _start(env)).result
    assert result["status"] == "partial_failed" and result["metadataSync"]["status"] == "failed"
    assert config.get()["channels"][0]["models"][-1]["real"] == "new-api"


def test_cancel_after_last_model_skips_metadata(env, monkeypatch):
    _install([_api()])
    env.responses["api-secret-alpha"] = _payload("new-api")
    finished = Event()
    metadata_calls = []
    async def metadata():
        metadata_calls.append(True)
        return {"status": "succeeded"}
    monkeypatch.setattr(model_pricing, "refresh_metadata_after_model_sync", metadata)
    def progress(operation_id, event):
        if event["phase"] == "done":
            env.store.cancel(env.context, operation_id)
        elif event["phase"] == "finished":
            finished.set()
    operation = env.control.start_upstream_sync(env.context, progress_sink=progress)
    _wait(env, operation)
    assert finished.wait(3) and metadata_calls == []
    assert config.get()["channels"][0]["models"][-1]["real"] == "new-api"


def test_model_batch_reconciles_actual_new_binding_without_changing_manual_values(env, monkeypatch):
    _install([_api()])
    manual_binding = {"target": "openai/gpt-5.4", "source": "manual"}
    overrides = {"defaults": {"gpt-5.4": {"contextWindow": 12345}}}
    config.update(lambda cfg: cfg.update(
        pricing={**cfg.get("pricing", {}), "enabled": True, "autoUpdate": True},
        modelBindings={"defaults": {"manual-alias": manual_binding}, "scoped": {}},
        modelMetadataOverrides=overrides,
    ))
    model_pricing.initialize()
    env.responses["api-secret-alpha"] = _payload("gpt-5.4")
    downloads = []
    monkeypatch.setattr(model_pricing, "refresh_metadata_after_model_sync", real_metadata_followup)
    monkeypatch.setattr(model_pricing, "refresh_remote_catalog_sync", lambda: downloads.append(True) or True)
    result = _wait(env, _start(env)).result
    saved = config.get()
    assert result["metadataSync"]["status"] == "succeeded" and downloads == [True]
    assert saved["modelBindings"]["defaults"]["gpt-5.4"]["target"] == "openai/gpt-5.4"
    assert saved["modelBindings"]["defaults"]["gpt-5.4"]["source"] == "auto"
    assert saved["modelBindings"]["defaults"]["manual-alias"] == manual_binding
    assert saved["modelMetadataOverrides"] == overrides


def test_progress_shows_metadata_phase_until_it_finishes(monkeypatch):
    monkeypatch.setattr(model_center_sync, "_paint", lambda *args: None)
    monkeypatch.setattr(model_center_sync, "_schedule_cleanup", lambda *args: None)
    monkeypatch.setattr(model_center_sync, "_EVENTS", {})
    sink = model_center_sync.make_sink(1, "mc:list")
    sink("op", {"phase": "done", "total": 1, "index": 0, "status": "succeeded", "models": ["m"]})
    sink("op", {"phase": "metadata_start", "total": 1})
    assert "正在统一更新元数据" in model_center_sync._progress_text("op")
    sink("op", {"phase": "metadata_done", "total": 1, "status": "partial_failed"})
    text = model_center_sync._progress_text("op")
    assert "正在统一更新元数据" not in text
    assert "拉取失败，已使用本地目录匹配" in text
